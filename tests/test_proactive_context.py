"""Tests for the proactive context surface (Phase 3a).

When a new session starts, the SessionStart hook reads the user's first
prompt and searches the digest corpus + themes + lessons for matching
prior work, then injects a `<brain-context>` block so the LLM has that
context BEFORE answering.

This is the killer feature — "the brain remembers what's relevant
without you having to ask `recall` first".

Contract pinned:
  - search(prompt, brain_root, k) returns a list of ProactiveHit
    sorted by score descending
  - Each hit carries title, source ("digest"/"theme"/"lesson"), a
    one-line summary, and the source path/id
  - Below score_threshold → not returned (don't pollute context with
    weak matches)
  - format_context_block(hits, max_tokens) returns the literal string
    to inject, capped at max_tokens (4-char estimate)
  - Empty hits → empty string (no surface, no overhead)
  - SessionStart hook integration tested via dispatch() entry point
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "tools"))


@pytest.fixture
def proactive_mod():
    import proactive_context
    return proactive_context


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_digest(brain_root: Path, sid: str, *,
                  title: str, learned: str,
                  tags: list[str], date: str = "2026-05-10") -> Path:
    """Write a synthetic digest markdown the proactive search can index."""
    md_dir = brain_root / "memory" / "semantic" / "digests"
    md_dir.mkdir(parents=True, exist_ok=True)
    tag_str = ", ".join(f'"{t}"' for t in tags)
    path = md_dir / f"{date}__{title.lower().replace(' ', '-')}__{sid}.md"
    path.write_text(f"""---
session_id: "{sid}"
source: claude
started_at: {date}T12:00:00Z
ended_at: {date}T13:00:00Z
domain_tags: [{tag_str}]
outcome: completed
salience: 7
---

# {title}

## What you did

Worked on this topic.

## What was learned

{learned}
""")
    return path


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

class TestSearch:
    def test_relevant_digest_surfaces_for_matching_prompt(self,
                                                          proactive_mod,
                                                          tmp_path):
        """A digest about 'payment-flow timeout' should surface for a
        prompt mentioning payment-flow / timeout."""
        _seed_digest(tmp_path, "s1",
                      title="Payment-flow timeout root cause",
                      learned="The 5s default was hit by the legacy "
                              "retry pattern.",
                      tags=["payment-flow", "timeouts"])
        hits = proactive_mod.search(
            "I'm seeing payment-flow timeouts again",
            brain_root=tmp_path, k=3,
        )
        assert len(hits) >= 1
        assert hits[0].source == "digest"
        assert "payment-flow" in hits[0].title.lower() or \
               "timeout" in hits[0].title.lower()
        assert hits[0].score > 0

    def test_irrelevant_digest_doesnt_surface(self, proactive_mod, tmp_path):
        """Below-threshold matches don't pollute context. Without this,
        every new session injects unrelated history into the prompt."""
        _seed_digest(tmp_path, "s1",
                      title="A completely unrelated topic about cats",
                      learned="Cats are not dogs.",
                      tags=["zoology", "felines"])
        hits = proactive_mod.search(
            "How do I run terraform apply",
            brain_root=tmp_path, k=3, score_threshold=0.1,
        )
        # Either no hits, or all are extremely weak. Pinning: nothing
        # above the threshold.
        for h in hits:
            assert h.score < 0.1, (
                f"unrelated digest scored {h.score} for terraform query"
            )

    def test_ranks_multiple_hits_by_score(self, proactive_mod, tmp_path):
        """Two relevant digests should both surface and be ordered."""
        _seed_digest(tmp_path, "s1",
                      title="Auth rewrite token leak",
                      learned="Tokens were being logged to stdout.",
                      tags=["auth-rewrite", "security"])
        _seed_digest(tmp_path, "s2",
                      title="Auth rewrite test coverage gaps",
                      learned="Missing tests on the refresh path.",
                      tags=["auth-rewrite", "testing"])
        hits = proactive_mod.search(
            "auth rewrite progress",
            brain_root=tmp_path, k=5,
        )
        assert len(hits) >= 2
        # Sorted descending
        scores = [h.score for h in hits]
        assert scores == sorted(scores, reverse=True)

    def test_returns_empty_when_no_digests(self, proactive_mod, tmp_path):
        hits = proactive_mod.search(
            "some prompt", brain_root=tmp_path, k=5,
        )
        assert hits == []

    def test_caps_to_k(self, proactive_mod, tmp_path):
        for i in range(10):
            _seed_digest(tmp_path, f"s{i}",
                          title=f"Investigation about widgets {i}",
                          learned=f"Widget insight {i}",
                          tags=["widgets"])
        hits = proactive_mod.search(
            "widget", brain_root=tmp_path, k=3,
        )
        assert len(hits) <= 3


# ---------------------------------------------------------------------------
# Context-block formatting
# ---------------------------------------------------------------------------

class TestContextBlock:
    def test_format_empty_hits_returns_empty(self, proactive_mod):
        block = proactive_mod.format_context_block([])
        assert block == ""

    def test_format_includes_each_hit(self, proactive_mod):
        from proactive_context import ProactiveHit
        hits = [
            ProactiveHit(title="A", source="digest",
                         summary="learned A", path="/p/a.md",
                         session_id="s1", date="2026-05-01",
                         score=0.9),
            ProactiveHit(title="B", source="digest",
                         summary="learned B", path="/p/b.md",
                         session_id="s2", date="2026-05-02",
                         score=0.7),
        ]
        block = proactive_mod.format_context_block(hits)
        assert "<brain-context>" in block
        assert "</brain-context>" in block
        assert "A" in block and "B" in block
        assert "learned A" in block
        # Each hit's date is visible for the user's quick scan
        assert "2026-05-01" in block

    def test_format_respects_max_tokens(self, proactive_mod):
        """When the formatted block would exceed max_tokens (4-char
        estimate), drop trailing hits until it fits."""
        from proactive_context import ProactiveHit
        hits = [
            ProactiveHit(title="A" * 200, source="digest",
                         summary="a" * 500, path="/p/a.md",
                         session_id="s1", date="2026-05-01", score=0.9),
            ProactiveHit(title="B" * 200, source="digest",
                         summary="b" * 500, path="/p/b.md",
                         session_id="s2", date="2026-05-02", score=0.8),
        ]
        block = proactive_mod.format_context_block(hits, max_tokens=50)
        # 50 tokens ≈ 200 chars; can't fit both. Should fit one or zero.
        assert len(block) // 4 <= 50 + 30  # generous slack for wrappers


# ---------------------------------------------------------------------------
# Dispatch (hook entry point)
# ---------------------------------------------------------------------------

class TestDispatch:
    def test_dispatch_with_prompt_returns_block(self, proactive_mod,
                                                 tmp_path):
        _seed_digest(tmp_path, "s1",
                      title="Payment-flow timeout root cause",
                      learned="Retry config was 0.",
                      tags=["payment-flow"])
        block = proactive_mod.dispatch(
            "looking at payment-flow timeout again",
            brain_root=tmp_path,
        )
        assert "<brain-context>" in block

    def test_dispatch_with_no_brain_returns_empty(self, proactive_mod,
                                                   tmp_path):
        """Brand-new install with no digests yet: hook must NOT inject
        an empty <brain-context> wrapper (that would confuse the LLM
        into thinking there was context when there wasn't)."""
        block = proactive_mod.dispatch(
            "anything",
            brain_root=tmp_path,
        )
        assert block == ""

    def test_dispatch_respects_disabled_config(self, proactive_mod,
                                                tmp_path, monkeypatch):
        """User can disable proactive injection entirely via env or
        config. Hook returns empty regardless of corpus state."""
        _seed_digest(tmp_path, "s1",
                      title="Some topic",
                      learned="Some insight.",
                      tags=["topic"])
        monkeypatch.setenv("BRAIN_PROACTIVE_DISABLED", "1")
        block = proactive_mod.dispatch(
            "talk about that topic",
            brain_root=tmp_path,
        )
        assert block == ""
