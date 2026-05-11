"""Tests for the PROFILE.md auto-builder (Phase 2a).

The profile builder reads all markdown digests under
`memory/semantic/digests/` and asks the LLM to produce a structured
user profile at `memory/semantic/PROFILE.md`. The profile is what
makes the brain "know" the user — it's the answer to "what does this
person work on?" Indexed by recall as a normal markdown doc.

Contract pinned here:
  - Reads ALL digest markdowns under semantic/digests/ (skips archived)
  - Builds an LLM prompt with the digest content; asks for structured
    JSON (domains, active_threads, recent_learnings, long_running_themes)
  - Renders the JSON to PROFILE.md with YAML front-matter + sections
  - Idempotent: tracks a content-SHA over the digest set; re-run is
    no-op if nothing changed
  - One bad digest doesn't break the build
  - Framework-pure: no hardcoded domain taxonomy
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "tools"))
sys.path.insert(0, str(REPO_ROOT / "agent" / "memory"))


@pytest.fixture
def profile_mod():
    import profile_builder
    return profile_builder


# ---------------------------------------------------------------------------
# Fake provider — returns canned profile JSON
# ---------------------------------------------------------------------------

class FakeProvider:
    name = "fake"
    default_model = "fake-1"

    def __init__(self, *, response: dict | None = None):
        self.calls = []
        self._response = response or {
            "domains": [
                {"tag": "auth-rewrite", "count": 3,
                 "last_seen": "2026-05-10",
                 "top_digests": ["t1", "t2", "t3"]},
                {"tag": "payment-flow", "count": 2,
                 "last_seen": "2026-05-09",
                 "top_digests": ["t4", "t5"]},
            ],
            "active_threads": [
                {"session_id": "s1", "title": "Investigating flaky tests",
                 "outcome": "in-progress", "last_seen": "2026-05-10"},
            ],
            "recent_learnings": [
                "Use the staging cluster for migration dry-runs",
                "TestContainers requires Docker; CI doesn't have it",
            ],
            "long_running_themes": [
                {"theme": "build-pipeline reliability",
                 "session_count": 7, "first_seen": "2026-04-01"},
            ],
        }

    def is_available(self):
        return (True, "")

    def invoke(self, system, prompt, *, model=None, json_schema=None,
               max_budget_usd=5.0, timeout_s=60):
        self.calls.append({"system": system, "prompt": prompt,
                           "schema": json_schema})
        from llm_providers.base import LLMResult
        return LLMResult(
            text=json.dumps(self._response),
            parsed_json=self._response,
            tokens_in=100, tokens_out=80,
            provider=self.name, model=model or self.default_model,
            cost_usd=None,
        )


# ---------------------------------------------------------------------------
# Fixtures: synthetic markdown digests
# ---------------------------------------------------------------------------

def _digest_md(sid: str, *, title: str = "Title", tags: list[str] | None = None,
               started: str = "2026-05-10T12:00:00Z",
               outcome: str = "completed",
               salience: int = 5,
               what_user_did: str = "Did work.",
               what_was_learned: str = "Learned a thing.") -> str:
    tags = tags or []
    tag_str = ", ".join(f'"{t}"' for t in tags)
    return f"""---
session_id: "{sid}"
source: claude
started_at: {started}
ended_at: {started}
cwd: "/tmp"
git_branch: "main"
project_slug: ""
model: ""
domain_tags: [{tag_str}]
outcome: {outcome}
salience: {salience}
---

# {title}

## What you did

{what_user_did}

## What was learned

{what_was_learned}

## Decisions

_(none recorded)_

## Files touched

_(none recorded)_
"""


def _seed_digests(brain_root: Path, n: int = 3) -> Path:
    md_dir = brain_root / "memory" / "semantic" / "digests"
    md_dir.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        path = md_dir / f"2026-05-0{i+1}__topic-{i}__sid{i:08d}.md"
        path.write_text(_digest_md(
            f"sid-{i}", title=f"Investigation {i}",
            tags=[f"topic-{i%2}", f"area-{i}"],
            started=f"2026-05-0{i+1}T12:00:00Z",
        ))
    return md_dir


# ---------------------------------------------------------------------------
# Build & render
# ---------------------------------------------------------------------------

class TestProfileBuild:
    def test_build_with_no_digests_returns_zero(self, profile_mod, tmp_path):
        """An empty digest dir is a valid state (new install, no
        backfill yet). build() must return cleanly with 0 digests
        processed and no LLM call."""
        provider = FakeProvider()
        stats = profile_mod.build(brain_root=tmp_path, provider=provider)
        assert stats["digests_read"] == 0
        assert stats["profile_written"] is False
        assert len(provider.calls) == 0

    def test_build_with_digests_writes_profile(self, profile_mod, tmp_path):
        _seed_digests(tmp_path, n=3)
        provider = FakeProvider()
        stats = profile_mod.build(brain_root=tmp_path, provider=provider)
        assert stats["digests_read"] == 3
        assert stats["profile_written"] is True
        prof = tmp_path / "memory" / "semantic" / "PROFILE.md"
        assert prof.exists()
        body = prof.read_text()
        assert body.startswith("---\n")
        # Sections present
        assert "# Profile" in body or "# User profile" in body
        assert "## Domains" in body
        assert "## Active threads" in body
        assert "## Recent learnings" in body
        assert "## Long-running themes" in body
        # Content from the fake response surfaces
        assert "auth-rewrite" in body
        assert "Investigating flaky tests" in body
        assert "staging cluster" in body

    def test_build_passes_full_schema_to_provider(self, profile_mod,
                                                   tmp_path):
        """The provider call must request all 4 sections via the JSON
        schema's required-keys list. Otherwise an LLM that returns
        partial data slips through."""
        _seed_digests(tmp_path, n=2)
        provider = FakeProvider()
        profile_mod.build(brain_root=tmp_path, provider=provider)
        assert len(provider.calls) == 1
        schema = provider.calls[0]["schema"]
        required = set(schema.get("required", []))
        for k in ("domains", "active_threads", "recent_learnings",
                  "long_running_themes"):
            assert k in required, f"missing required key {k!r}"

    def test_build_includes_digest_content_in_prompt(self, profile_mod,
                                                      tmp_path):
        """The LLM must see the actual digest bodies (not just titles)
        so it can derive themes and learnings. Prove the prompt carries
        a recognizable fragment from each digest."""
        _seed_digests(tmp_path, n=3)
        provider = FakeProvider()
        profile_mod.build(brain_root=tmp_path, provider=provider)
        prompt = provider.calls[0]["prompt"]
        # Each digest's title should be in the prompt
        for i in range(3):
            assert f"Investigation {i}" in prompt

    def test_build_idempotent_when_unchanged(self, profile_mod, tmp_path):
        """A second build with no digest changes is a no-op (no LLM
        call, no rewrite). Tracking via a content-SHA of the digest set
        stored in the profile front-matter or a sidecar."""
        _seed_digests(tmp_path, n=2)
        provider = FakeProvider()
        s1 = profile_mod.build(brain_root=tmp_path, provider=provider)
        s2 = profile_mod.build(brain_root=tmp_path, provider=provider)
        assert s1["profile_written"] is True
        assert s2["profile_written"] is False
        # Provider only called once
        assert len(provider.calls) == 1

    def test_build_rebuilds_when_digests_change(self, profile_mod, tmp_path):
        """Adding a new digest must trigger a rebuild on the next
        build() call."""
        _seed_digests(tmp_path, n=2)
        provider = FakeProvider()
        profile_mod.build(brain_root=tmp_path, provider=provider)
        # Add a third
        md_dir = tmp_path / "memory" / "semantic" / "digests"
        (md_dir / "new.md").write_text(_digest_md("new-sid",
                                                   title="New work",
                                                   tags=["new-area"]))
        s2 = profile_mod.build(brain_root=tmp_path, provider=provider)
        assert s2["profile_written"] is True
        assert len(provider.calls) == 2


# ---------------------------------------------------------------------------
# Resilience
# ---------------------------------------------------------------------------

class TestResilience:
    def test_malformed_digest_does_not_abort(self, profile_mod, tmp_path):
        """A digest file with broken YAML or unreadable bytes must be
        skipped, NOT halt the build. The remaining good digests still
        contribute to the profile."""
        md_dir = tmp_path / "memory" / "semantic" / "digests"
        md_dir.mkdir(parents=True)
        # one good, one broken
        (md_dir / "good.md").write_text(_digest_md("ok-sid", title="OK"))
        (md_dir / "broken.md").write_text("---\nthis is not yaml: : :\n---")
        provider = FakeProvider()
        stats = profile_mod.build(brain_root=tmp_path, provider=provider)
        # At least the good one counts
        assert stats["digests_read"] >= 1
        assert stats["profile_written"] is True

    def test_provider_failure_does_not_corrupt_profile(self, profile_mod,
                                                       tmp_path):
        """If the LLM call fails, the existing PROFILE.md must remain
        intact. Don't write a half-baked replacement."""
        _seed_digests(tmp_path, n=2)
        prof = tmp_path / "memory" / "semantic" / "PROFILE.md"
        prof.parent.mkdir(parents=True, exist_ok=True)
        prof.write_text("# Pre-existing profile\n\nuser content here\n")

        from llm_providers.base import LLMError

        class FailProvider:
            name = "fail"
            default_model = "x"
            def is_available(self): return (True, "")
            def invoke(self, *a, **k):
                raise LLMError("simulated failure")

        with pytest.raises(LLMError):
            profile_mod.build(brain_root=tmp_path, provider=FailProvider())
        # Existing profile NOT overwritten
        assert "Pre-existing profile" in prof.read_text()


# ---------------------------------------------------------------------------
# Framework purity
# ---------------------------------------------------------------------------

class TestFrameworkPurity:
    def test_no_hardcoded_org_in_system_prompt(self, profile_mod):
        """The system prompt must not encode any specific company,
        domain, or stack — the profile should reflect what's in the
        DIGESTS, not a pre-baked taxonomy."""
        system = profile_mod.SYSTEM_PROMPT
        forbidden = ["veho", "shipveho", "anthropic.com", "openai.com"]
        lower = system.lower()
        for f in forbidden:
            assert f not in lower, f"system prompt contains {f!r}"
