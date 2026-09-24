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


# ---------- supersession chains (Phase 2) ---------------------------------


def _seed_jsonl(root: Path, rows: list[dict]) -> None:
    import json
    p = root / "memory" / "semantic" / "lessons.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_trace_renders_supersession_chain(tmp_path):
    from recall.cli import app

    root = _brain(tmp_path)
    _write_lesson(
        root, "lesson_old",
        "name: lesson_old\n"
        "type: lesson\n"
        "status: superseded\n"
        "valid_from: 2025-01-01T00:00:00Z\n"
        "valid_until: 2026-06-01T00:00:00Z\n"
        "superseded_by: lesson_new",
    )
    _write_lesson(
        root, "lesson_new",
        "name: lesson_new\n"
        "type: lesson\n"
        "status: current\n"
        "valid_from: 2026-06-01T00:00:00Z\n"
        "supersedes: lesson_old",
    )
    res = runner.invoke(app, ["trace", "lesson_old", "--brain-root", str(root)])
    assert res.exit_code == 0, res.output
    out = res.output
    assert "supersession chain:" in out
    assert "lesson_old" in out and "lesson_new" in out
    assert "status=superseded" in out
    assert "status=current" in out
    assert "→ lesson_new" in out
    assert "valid_from=2025-01-01" in out


def test_trace_walks_jsonl_when_companion_missing(tmp_path):
    """Chain links resolve against lessons.jsonl when no companion
    markdown exists for the successor id."""
    from recall.cli import app

    root = _brain(tmp_path)
    _write_lesson(
        root, "lesson_old",
        "name: lesson_old\n"
        "type: lesson\n"
        "status: superseded\n"
        "valid_from: 2025-01-01T00:00:00Z\n"
        "superseded_by: lesson_jsonl_only",
    )
    _seed_jsonl(root, [{
        "id": "lesson_jsonl_only",
        "claim": "the newer rule",
        "status": "accepted",
        "valid_from": "2026-06-01T00:00:00Z",
        "accepted_at": "2026-06-01T00:00:00Z",
        "reviewer": "host-agent",
        "rationale": "test",
        "supersedes": "lesson_old",
    }])
    res = runner.invoke(app, ["trace", "lesson_old", "--brain-root", str(root)])
    assert res.exit_code == 0, res.output
    assert "lesson_jsonl_only" in res.output
    assert "status=current" in res.output  # jsonl 'accepted' aliases to current


def test_trace_cycle_does_not_loop(tmp_path):
    from recall.cli import app

    root = _brain(tmp_path)
    # A → B → A cycle must terminate, not hang.
    _write_lesson(root, "lesson_a",
                  "name: lesson_a\ntype: lesson\nstatus: superseded\nsuperseded_by: lesson_b")
    _write_lesson(root, "lesson_b",
                  "name: lesson_b\ntype: lesson\nstatus: superseded\nsuperseded_by: lesson_a")
    res = runner.invoke(app, ["trace", "lesson_a", "--brain-root", str(root)])
    assert res.exit_code == 0, res.output
    assert "supersession chain:" in res.output


def test_trace_bare_lesson_unchanged(tmp_path):
    """Pre-0.6 bare lesson: provenance-none path, no chain section."""
    from recall.cli import app

    root = _brain(tmp_path)
    _write_lesson(root, "bare-lesson",
                  "name: bare-lesson\ndescription: legacy lesson with no provenance")
    res = runner.invoke(app, ["trace", "bare-lesson", "--brain-root", str(root)])
    assert res.exit_code == 0, res.output
    assert "none" in res.output.lower()
    assert "supersession chain" not in res.output


def test_trace_lesson_without_chain_prints_no_chain_section(tmp_path):
    """A current lesson with provenance but no supersession links keeps
    the old output shape (no empty chain header)."""
    from recall.cli import app

    root = _brain(tmp_path)
    _write_lesson(
        root, "plain-lesson",
        "name: plain-lesson\n"
        "type: lesson\n"
        "source: recall-remember\n"
        "created_by: recall-remember\n"
        "status: current\n"
        "valid_from: 2026-06-01T00:00:00Z",
    )
    res = runner.invoke(app, ["trace", "plain-lesson", "--brain-root", str(root)])
    assert res.exit_code == 0, res.output
    assert "supersession chain" not in res.output


def test_provenance_label_includes_temporal_suffix():
    from recall.sanitize import provenance_label
    label = provenance_label({
        "created_by": "graduate",
        "status": "superseded",
        "valid_from": "2025-01-01T00:00:00Z",
    })
    assert "graduate" in label
    assert "superseded" in label
    assert "valid_from=2025-01-01" in label
    assert len(label) <= 120


def test_provenance_label_unchanged_without_temporal_fields():
    from recall.sanitize import provenance_label
    assert provenance_label({"created_by": "recall-remember"}) == "recall-remember"
    assert provenance_label(None) == "none"
