"""Gap T: `recall trace` provenance command.

The audit flagged that a recalled lesson carries a confidence/provenance label
with no trail back to the source that produced it. Provenance frontmatter is
now written, but a reader still had no command to walk it. `recall trace
<lesson>` reads a lesson's frontmatter and prints its provenance chain:
source, created_by, session_id, reviewed_by / needs_review, evidence_ids,
source_candidate, and pointers to the originating digest/candidate when those
files exist in the brain.

Hermetic: builds a tiny brain on tmp_path, no Qdrant or embedder.
"""
from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

runner = CliRunner()


def _brain(tmp_path: Path) -> Path:
    root = tmp_path / ".agent"
    (root / "memory" / "semantic" / "lessons").mkdir(parents=True)
    (root / "memory" / "semantic" / "digests").mkdir(parents=True)
    return root


def _write_lesson(root: Path, slug: str, frontmatter: str, body: str = "Body text.") -> Path:
    p = root / "memory" / "semantic" / "lessons" / f"{slug}.md"
    p.write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    return p


def test_trace_prints_provenance_chain_for_staged_lesson(tmp_path):
    from recall.cli import app

    root = _brain(tmp_path)
    _write_lesson(
        root,
        "staged-lesson",
        "name: staged-lesson\n"
        "description: a staged lesson\n"
        "type: lesson\n"
        "source: recall-remember\n"
        "created_by: recall-remember\n"
        "provenance: agent\n"
        "created: 2026-06-10T00:00:00+00:00\n"
        "session_id: sess-abc123\n"
        "needs_review: true\n"
        "review_reason: unreviewed-remember",
    )
    res = runner.invoke(app, ["trace", "staged-lesson", "--brain-root", str(root)])
    assert res.exit_code == 0, res.output
    out = res.output
    assert "recall-remember" in out          # source / created_by
    assert "sess-abc123" in out               # session id
    assert "agent" in out                     # provenance label
    # The staged (unreviewed) status must be visible to the reader.
    assert "needs_review" in out or "unreviewed" in out.lower()


def test_trace_shows_reviewed_by_for_durable_lesson(tmp_path):
    from recall.cli import app

    root = _brain(tmp_path)
    _write_lesson(
        root,
        "durable-lesson",
        "name: durable-lesson\n"
        "description: a durable lesson\n"
        "type: lesson\n"
        "source: recall-remember\n"
        "created_by: recall-remember\n"
        "reviewed_by: human-cli\n"
        "created: 2026-06-10T00:00:00+00:00",
    )
    res = runner.invoke(app, ["trace", "durable-lesson", "--brain-root", str(root)])
    assert res.exit_code == 0, res.output
    assert "human-cli" in res.output


def test_trace_reports_no_provenance_for_bare_lesson(tmp_path):
    from recall.cli import app

    root = _brain(tmp_path)
    # A v0.5-era lesson with only name/description (no provenance fields).
    _write_lesson(
        root,
        "bare-lesson",
        "name: bare-lesson\ndescription: legacy lesson with no provenance",
    )
    res = runner.invoke(app, ["trace", "bare-lesson", "--brain-root", str(root)])
    assert res.exit_code == 0, res.output
    # Must be honest that there is no provenance trail, not crash.
    assert "none" in res.output.lower() or "no provenance" in res.output.lower()


def test_trace_links_to_originating_digest_when_present(tmp_path):
    from recall.cli import app

    root = _brain(tmp_path)
    # A digest file whose name embeds the session id the lesson references.
    digest = (
        root / "memory" / "semantic" / "digests"
        / "2026-06-09__some-session__sess-abc123.md"
    )
    digest.write_text("---\nsession_id: sess-abc123\n---\n\nDigest body.\n", encoding="utf-8")
    _write_lesson(
        root,
        "linked-lesson",
        "name: linked-lesson\n"
        "description: links to a digest\n"
        "source: recall-remember\n"
        "session_id: sess-abc123",
    )
    res = runner.invoke(app, ["trace", "linked-lesson", "--brain-root", str(root)])
    assert res.exit_code == 0, res.output
    # The originating digest should be surfaced as a pointer.
    assert "sess-abc123" in res.output
    assert "digest" in res.output.lower()


def test_trace_unknown_target_exits_nonzero_with_message(tmp_path):
    from recall.cli import app

    root = _brain(tmp_path)
    res = runner.invoke(app, ["trace", "does-not-exist", "--brain-root", str(root)])
    assert res.exit_code != 0
    assert "does-not-exist" in res.output or "no lesson" in res.output.lower()


def test_trace_accepts_a_file_path(tmp_path):
    from recall.cli import app

    root = _brain(tmp_path)
    p = _write_lesson(
        root,
        "by-path",
        "name: by-path\ndescription: addressed by path\nsource: recall-remember",
    )
    res = runner.invoke(app, ["trace", str(p), "--brain-root", str(root)])
    assert res.exit_code == 0, res.output
    assert "recall-remember" in res.output
