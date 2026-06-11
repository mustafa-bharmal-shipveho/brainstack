"""Tests for the digest renderer.

The renderer takes a structured digest dict + session metadata and writes
TWO surfaces:

  A. An episodic JSONL line at memory/episodic/digests/AGENT_LEARNINGS.jsonl
  B. A markdown file at memory/semantic/digests/<YYYY-MM-DD>__<slug>.md
     with YAML front-matter holding the structured fields.

Why both: episodic gives fast short-payload vector recall; markdown gives
human-browseable long-form + git-syncable. Recall finds digests through
either surface independently.

This file pins the exact shape so a future change can't silently break
either path. No org-specific identifiers in fixtures.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "tools"))


@pytest.fixture
def render_mod():
    import _digest_render
    return _digest_render


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_DIGEST = {
    "title": "Investigate intermittent failure in nightly build",
    "domain_tags": ["build-pipeline", "ci-flakes"],
    "what_user_did": "Ran the failing job locally, reproduced the race "
                     "by tightening a sleep, narrowed it to a fixture "
                     "teardown order.",
    "what_was_learned": "Fixture teardown must release the shared port "
                        "before the next test acquires it; otherwise "
                        "the listener leak surfaces as a flake.",
    "decisions": [
        "Add an explicit close-and-wait in the teardown",
        "Mark the test serial in the CI config",
    ],
    "files_touched": [
        "tests/conftest.py",
        ".github/workflows/nightly.yml",
    ],
    "outcome": "completed",
    "salience": 8,
}

SAMPLE_META = {
    "session_id": "sess-123",
    "source": "claude",
    "started_at": "2026-05-01T12:00:00Z",
    "ended_at":   "2026-05-01T13:30:00Z",
    "cwd": "/Users/u/dev/svc",
    "git_branch": "fix/nightly-flake",
    "project_slug": "svc",
    "model": "claude-haiku-4-5",
}


# ---------------------------------------------------------------------------
# Episodic line
# ---------------------------------------------------------------------------

class TestEpisodicRender:
    def test_returns_episode_with_existing_schema(self, render_mod):
        ep = render_mod.render_episodic(SAMPLE_DIGEST, SAMPLE_META)
        # Must match the shape that claude_session_adapter produces, so
        # downstream consumers (recall index, cluster.py) don't have to
        # special-case digests.
        for k in ("timestamp", "skill", "action", "result",
                  "detail", "pain_score", "importance", "reflection",
                  "confidence", "source", "origin", "summary"):
            assert k in ep, f"missing field {k!r}"

    def test_action_is_digest_title(self, render_mod):
        ep = render_mod.render_episodic(SAMPLE_DIGEST, SAMPLE_META)
        assert ep["action"] == SAMPLE_DIGEST["title"]

    def test_reflection_is_what_was_learned(self, render_mod):
        ep = render_mod.render_episodic(SAMPLE_DIGEST, SAMPLE_META)
        assert ep["reflection"] == SAMPLE_DIGEST["what_was_learned"]

    def test_summary_is_what_user_did(self, render_mod):
        ep = render_mod.render_episodic(SAMPLE_DIGEST, SAMPLE_META)
        assert ep["summary"] == SAMPLE_DIGEST["what_user_did"]

    def test_origin_namespaces_by_source(self, render_mod):
        ep = render_mod.render_episodic(SAMPLE_DIGEST, SAMPLE_META)
        assert ep["origin"] == "session.digest.claude"
        ep2 = render_mod.render_episodic(SAMPLE_DIGEST,
                                          {**SAMPLE_META, "source": "codex"})
        assert ep2["origin"] == "session.digest.codex"

    def test_conditions_populated_from_domain_tags(self, render_mod):
        """Domain tags act as the conditions field so cluster.py groups
        digests by topic. Without this, every digest is its own
        singleton cluster."""
        ep = render_mod.render_episodic(SAMPLE_DIGEST, SAMPLE_META)
        cond = ep.get("conditions", [])
        for tag in SAMPLE_DIGEST["domain_tags"]:
            assert tag in cond

    def test_source_metadata_captures_session_provenance(self, render_mod):
        ep = render_mod.render_episodic(SAMPLE_DIGEST, SAMPLE_META)
        src = ep["source"]
        assert src["adapter"] == "session-digest"
        assert src["session_id"] == "sess-123"
        assert src["project_slug"] == "svc"

    def test_pain_and_importance_derive_from_salience(self, render_mod):
        ep = render_mod.render_episodic(SAMPLE_DIGEST, SAMPLE_META)
        # salience=8 should map to a meaningful importance
        assert ep["importance"] >= 7


# ---------------------------------------------------------------------------
# Markdown render
# ---------------------------------------------------------------------------

class TestMarkdownRender:
    def test_path_is_date_and_title_slug(self, render_mod):
        path = render_mod.markdown_path_for(SAMPLE_DIGEST, SAMPLE_META,
                                             base_dir=Path("/x/y"))
        # YYYY-MM-DD__<slug>.md — derived from started_at + title
        assert path.parent == Path("/x/y")
        assert path.name.startswith("2026-05-01__")
        assert path.suffix == ".md"
        # Slug strips spaces + lowercases — must be filesystem-safe
        assert " " not in path.name
        assert path.name == path.name.lower()

    def test_path_collisions_unique_per_session(self, render_mod):
        """Two sessions on the same day with identical title-derived
        slugs must not clobber each other. Path should include something
        session-specific (short session_id suffix)."""
        path1 = render_mod.markdown_path_for(
            SAMPLE_DIGEST, SAMPLE_META, base_dir=Path("/x"))
        path2 = render_mod.markdown_path_for(
            SAMPLE_DIGEST, {**SAMPLE_META, "session_id": "sess-456"},
            base_dir=Path("/x"))
        assert path1 != path2

    def test_markdown_body_includes_front_matter_and_sections(self,
                                                              render_mod):
        body = render_mod.render_markdown(SAMPLE_DIGEST, SAMPLE_META)
        # Front matter
        assert body.startswith("---\n")
        # YAML keys
        assert "session_id: sess-123" in body
        assert "source: claude" in body
        assert "started_at: 2026-05-01T12:00:00Z" in body
        assert "domain_tags:" in body
        assert "build-pipeline" in body and "ci-flakes" in body
        assert "outcome: completed" in body
        assert "salience: 8" in body
        # Sections
        assert "# Investigate intermittent failure" in body
        assert "## What you did" in body
        assert SAMPLE_DIGEST["what_user_did"] in body
        assert "## What was learned" in body
        assert SAMPLE_DIGEST["what_was_learned"] in body
        assert "## Decisions" in body
        for d in SAMPLE_DIGEST["decisions"]:
            assert d in body
        assert "## Files touched" in body
        for f in SAMPLE_DIGEST["files_touched"]:
            assert f in body

    def test_front_matter_is_valid_yaml(self, render_mod):
        body = render_mod.render_markdown(SAMPLE_DIGEST, SAMPLE_META)
        # Extract front matter and parse
        parts = body.split("---\n", 2)
        assert len(parts) >= 3, "must have opening ---, content, closing ---"
        front = parts[1]
        try:
            import yaml
            data = yaml.safe_load(front)
        except ImportError:
            # PyYAML may not be installed; skip the YAML-parse assertion
            pytest.skip("PyYAML not available for full validation")
            return
        assert data["session_id"] == "sess-123"
        assert data["domain_tags"] == ["build-pipeline", "ci-flakes"]


# ---------------------------------------------------------------------------
# Dual-surface write (atomic)
# ---------------------------------------------------------------------------

class TestDualWrite:
    def test_write_dual_emits_both_files(self, render_mod, tmp_path):
        episodic_path = tmp_path / "ep" / "AGENT_LEARNINGS.jsonl"
        md_dir = tmp_path / "md"

        result = render_mod.write_dual(SAMPLE_DIGEST, SAMPLE_META,
                                        episodic_path=episodic_path,
                                        markdown_dir=md_dir)
        # Both paths returned for downstream logging
        assert "episodic_path" in result and "markdown_path" in result
        assert episodic_path.exists()
        assert Path(result["markdown_path"]).exists()
        # Episodic file has exactly one JSON line
        lines = [l for l in episodic_path.read_text().splitlines() if l.strip()]
        assert len(lines) == 1
        ep = json.loads(lines[0])
        assert ep["action"] == SAMPLE_DIGEST["title"]
        # Markdown has the title body
        md = Path(result["markdown_path"]).read_text()
        assert "# Investigate intermittent failure" in md

    def test_write_dual_appends_idempotently(self, render_mod, tmp_path):
        """Two write_dual calls for the SAME digest must produce two
        episodic lines (append-only by design — recall handles dedup),
        but only ONE markdown file (path is deterministic from
        session_id + date so the second write overwrites)."""
        episodic_path = tmp_path / "ep" / "AGENT_LEARNINGS.jsonl"
        md_dir = tmp_path / "md"
        render_mod.write_dual(SAMPLE_DIGEST, SAMPLE_META,
                              episodic_path=episodic_path,
                              markdown_dir=md_dir)
        render_mod.write_dual(SAMPLE_DIGEST, SAMPLE_META,
                              episodic_path=episodic_path,
                              markdown_dir=md_dir)
        lines = [l for l in episodic_path.read_text().splitlines() if l.strip()]
        assert len(lines) == 2
        # Only one markdown
        mds = list(md_dir.glob("*.md"))
        assert len(mds) == 1

    def test_carry_needs_review_preserves_quoted_flag(self, render_mod, tmp_path):
        # _existing_needs_review must recognize a QUOTED truthy flag (consistent
        # with recall.core / recall.lint), else re-render silently drops it.
        episodic_path = tmp_path / "ep" / "AGENT_LEARNINGS.jsonl"
        md_dir = tmp_path / "md"
        result = render_mod.write_dual(SAMPLE_DIGEST, SAMPLE_META,
                                       episodic_path=episodic_path, markdown_dir=md_dir)
        md_path = Path(result["markdown_path"])
        text = md_path.read_text()
        text = text.replace("---\n", "---\nneeds_review: 'yes'\n", 1)  # QUOTED form
        md_path.write_text(text)
        render_mod.write_dual(SAMPLE_DIGEST, SAMPLE_META,
                              episodic_path=episodic_path, markdown_dir=md_dir)
        assert "needs_review" in md_path.read_text(), "re-render dropped a quoted review flag"

    def test_yaml_safe_quotes_leading_at(self, render_mod):
        import yaml
        digest = dict(SAMPLE_DIGEST, outcome="@oncall paged; resolved in 20m")
        md = render_mod.render_markdown(digest, SAMPLE_META)
        parsed = yaml.safe_load(md.split("---", 2)[1])  # must not raise
        assert parsed["outcome"] == "@oncall paged; resolved in 20m"

    def test_outcome_with_colon_yields_parseable_frontmatter(self, render_mod):
        """Regression: an `outcome` containing a colon must not break YAML
        (it used to make the whole frontmatter unparseable → empty fm →
        type filter, needs_review, etc. all invisible)."""
        import yaml
        digest = dict(SAMPLE_DIGEST,
                      outcome="Scope negotiated: 502 backend error deferred to triage")
        md = render_mod.render_markdown(digest, SAMPLE_META)
        block = md.split("---", 2)[1]
        parsed = yaml.safe_load(block)
        assert isinstance(parsed, dict)
        assert parsed["outcome"] == "Scope negotiated: 502 backend error deferred to triage"

    def test_write_dual_preserves_needs_review_across_rerender(self, render_mod, tmp_path):
        """If a human / `recall lint --mark` flagged a digest with
        needs_review, re-rendering the SAME digest must NOT silently drop
        the flag (it overwrites by deterministic path)."""
        episodic_path = tmp_path / "ep" / "AGENT_LEARNINGS.jsonl"
        md_dir = tmp_path / "md"
        result = render_mod.write_dual(SAMPLE_DIGEST, SAMPLE_META,
                                       episodic_path=episodic_path,
                                       markdown_dir=md_dir)
        md_path = Path(result["markdown_path"])
        # Simulate lint --mark adding the flag into the frontmatter.
        text = md_path.read_text()
        text = text.replace("---\n", "---\nneeds_review: true\n", 1)
        md_path.write_text(text)
        assert "needs_review: true" in md_path.read_text()

        # Re-render the same digest (same path → overwrite).
        render_mod.write_dual(SAMPLE_DIGEST, SAMPLE_META,
                              episodic_path=episodic_path,
                              markdown_dir=md_dir)
        after = md_path.read_text()
        assert "needs_review: true" in after, "re-render dropped the review flag"
        # Idempotent: exactly one occurrence, not duplicated.
        assert after.count("needs_review:") == 1
