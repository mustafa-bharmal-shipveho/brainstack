"""The digest renderer must emit `name` / `description` / `type` itself.

Why this file exists as a separate pin
--------------------------------------
`write_dual` overwrites a digest at a deterministic path every time the
source session's sha changes, and it carries only `needs_review` across
that rewrite. So a `recall lint --fix-digests` backfill is silently undone
the next time the session is re-digested — unless the renderer produces
the same three keys on its own.

That makes this a seam test, not a formatting test: the backfill (lint)
and the writer (the digest adapter) have to agree on the contract, or the
brain oscillates between having and not having the keys recall indexes on.

`tests/test_digest_render.py` owns the rest of the renderer contract.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "tools"))


@pytest.fixture
def render_mod():
    import _digest_render

    return _digest_render


SAMPLE_DIGEST = {
    "title": "Investigate intermittent failure in nightly build",
    "domain_tags": ["build-pipeline", "ci-flakes"],
    "what_user_did": "Ran the failing job locally, reproduced the race by "
                     "tightening a sleep, narrowed it to a fixture teardown "
                     "order. Then wrote the regression test.",
    "what_was_learned": "Fixture teardown must release the shared port.",
    "decisions": ["Add an explicit close-and-wait in the teardown"],
    "files_touched": ["tests/conftest.py"],
    "outcome": "completed",
    "salience": 8,
}

SAMPLE_META = {
    "session_id": "sess-12345678",
    "source": "claude",
    "started_at": "2026-05-01T12:00:00Z",
    "ended_at": "2026-05-01T13:30:00Z",
    "cwd": "/work/svc",
    "git_branch": "fix/nightly-flake",
    "project_slug": "svc",
    "model": "claude-haiku-4-5",
}


def _front(md: str) -> dict:
    return yaml.safe_load(md.split("---\n", 2)[1])


def _front_keys_in_order(md: str) -> list[str]:
    block = md.split("---\n", 2)[1]
    return [line.split(":", 1)[0] for line in block.splitlines() if ":" in line]


# ---------------------------------------------------------------------------
# Pin 1 — the renderer emits all three keys
# ---------------------------------------------------------------------------

def test_render_markdown_emits_name_description_type(render_mod):
    md = render_mod.render_markdown(
        SAMPLE_DIGEST, SAMPLE_META, name="2026-05-01__nightly-flake__12345678")

    assert md.startswith("---\n")
    # They lead the block, so a human scanning the file sees the identity
    # fields before the session plumbing.
    assert _front_keys_in_order(md)[:3] == ["name", "description", "type"]

    data = _front(md)
    assert data["type"] == "digest"
    assert data["name"] == "2026-05-01__nightly-flake__12345678"
    # Description = title, an em dash, then the FIRST sentence of what the
    # user did — the same shape `recall lint --fix-digests` backfills.
    assert data["description"].startswith(SAMPLE_DIGEST["title"])
    assert " — " in data["description"]
    assert "Ran the failing job locally" in data["description"]
    assert "Then wrote the regression test" not in data["description"]

    # The pre-existing block is untouched.
    assert data["session_id"] == "sess-12345678"
    assert data["domain_tags"] == ["build-pipeline", "ci-flakes"]
    assert data["outcome"] == "completed"
    assert data["salience"] == 8


def test_name_defaults_to_the_title_slug(render_mod):
    md = render_mod.render_markdown(SAMPLE_DIGEST, SAMPLE_META)
    assert _front(md)["name"] == render_mod._slugify(SAMPLE_DIGEST["title"])


def test_description_is_capped_at_200_on_a_word_boundary(render_mod):
    digest = dict(SAMPLE_DIGEST,
                  title="A " + " ".join(["very"] * 20) + " long title",
                  what_user_did=" ".join(["worked"] * 60) + ".")
    desc = _front(render_mod.render_markdown(digest, SAMPLE_META))["description"]
    assert len(desc) <= 200
    assert desc.endswith("…")
    assert not desc[:-1].endswith(" ")


def test_description_with_a_colon_keeps_the_block_parseable(render_mod):
    """Titles routinely read `Roadmap Setup: Linear Projects`. Unquoted,
    that colon makes the whole frontmatter unparseable, which silently
    drops type, tags and needs_review at index time."""
    digest = dict(SAMPLE_DIGEST, title="Roadmap Setup: Linear Projects and Gantt")
    data = _front(render_mod.render_markdown(digest, SAMPLE_META))
    assert isinstance(data, dict)
    assert data["description"].startswith("Roadmap Setup: Linear Projects and Gantt")
    assert data["type"] == "digest"


# ---------------------------------------------------------------------------
# Pin 2 — a re-digest does not undo the backfill
# ---------------------------------------------------------------------------

def test_backfill_then_rerender_keeps_keys(render_mod, tmp_path):
    episodic = tmp_path / "ep" / "AGENT_LEARNINGS.jsonl"
    md_dir = tmp_path / "md"

    result = render_mod.write_dual(SAMPLE_DIGEST, SAMPLE_META,
                                   episodic_path=episodic, markdown_dir=md_dir)
    md_path = Path(result["markdown_path"])

    first = _front(md_path.read_text(encoding="utf-8"))
    assert first["type"] == "digest"
    # write_dual names the digest after the file it writes, so the name a
    # wikilink resolves matches the filename on disk.
    assert first["name"] == md_path.stem
    assert first["description"]

    # Re-digest the same session (same deterministic path → overwrite).
    render_mod.write_dual(SAMPLE_DIGEST, SAMPLE_META,
                          episodic_path=episodic, markdown_dir=md_dir)

    second = _front(md_path.read_text(encoding="utf-8"))
    assert second["type"] == "digest", "re-render dropped the backfilled type"
    assert second["name"] == md_path.stem
    assert second["description"] == first["description"]


def test_rerender_does_not_duplicate_the_keys(render_mod, tmp_path):
    episodic = tmp_path / "ep" / "AGENT_LEARNINGS.jsonl"
    md_dir = tmp_path / "md"
    result = render_mod.write_dual(SAMPLE_DIGEST, SAMPLE_META,
                                   episodic_path=episodic, markdown_dir=md_dir)
    md_path = Path(result["markdown_path"])
    render_mod.write_dual(SAMPLE_DIGEST, SAMPLE_META,
                          episodic_path=episodic, markdown_dir=md_dir)
    keys = _front_keys_in_order(md_path.read_text(encoding="utf-8"))
    for key in ("name", "description", "type"):
        assert keys.count(key) == 1, f"{key} appears {keys.count(key)}x after re-render"


def test_rerender_still_preserves_needs_review(render_mod, tmp_path):
    """Regression guard for the interaction: adding three keys to the top
    of the block must not break the existing needs_review carry-across."""
    episodic = tmp_path / "ep" / "AGENT_LEARNINGS.jsonl"
    md_dir = tmp_path / "md"
    result = render_mod.write_dual(SAMPLE_DIGEST, SAMPLE_META,
                                   episodic_path=episodic, markdown_dir=md_dir)
    md_path = Path(result["markdown_path"])
    md_path.write_text(
        md_path.read_text(encoding="utf-8").replace(
            "---\n", "---\nneeds_review: true\n", 1),
        encoding="utf-8")

    render_mod.write_dual(SAMPLE_DIGEST, SAMPLE_META,
                          episodic_path=episodic, markdown_dir=md_dir)
    after = md_path.read_text(encoding="utf-8")
    assert after.count("needs_review:") == 1
    assert _front(after)["type"] == "digest"
