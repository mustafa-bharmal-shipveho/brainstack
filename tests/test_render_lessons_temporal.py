"""Temporal-validity behavior for render_lessons (Phase 2).

Covers `update_lesson` (in-place jsonl mutation for review-accepted
supersession) and rendering of persisted temporal fields.
"""
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "memory"))

import render_lessons  # noqa: E402


def _semantic(tmp_path: Path) -> str:
    semantic = tmp_path / "memory" / "semantic"
    semantic.mkdir(parents=True, exist_ok=True)
    return str(semantic)


def _lesson(lid, claim, status="accepted", **extra):
    row = {
        "id": lid,
        "claim": claim,
        "conditions": [],
        "evidence_ids": [],
        "status": status,
        "accepted_at": "2026-01-01T00:00:00Z",
        "reviewer": "host-agent",
        "rationale": "test",
        "supersedes": None,
    }
    row.update(extra)
    return row


def _rows(semantic_dir):
    return [
        json.loads(line)
        for line in (Path(semantic_dir) / "lessons.jsonl").read_text().splitlines()
        if line.strip()
    ]


def test_update_lesson_sets_superseded(tmp_path):
    semantic = _semantic(tmp_path)
    render_lessons.append_lesson(_lesson("lesson_old", "use the old way"), semantic)
    render_lessons.append_lesson(_lesson("lesson_new", "use the new way"), semantic)

    updated = render_lessons.update_lesson(
        "lesson_old", semantic,
        status="superseded",
        superseded_by="lesson_new",
        valid_until="2026-06-01T00:00:00Z",
    )
    assert updated is not None
    assert updated["status"] == "superseded"
    assert updated["superseded_by"] == "lesson_new"
    assert updated["valid_until"] == "2026-06-01T00:00:00Z"

    rows = _rows(semantic)
    # In-place update: still exactly two rows, no appended duplicate.
    assert len(rows) == 2
    old = [r for r in rows if r["id"] == "lesson_old"]
    assert len(old) == 1
    assert old[0]["status"] == "superseded"
    # Untouched row is byte-identical in content.
    new = [r for r in rows if r["id"] == "lesson_new"][0]
    assert new["status"] == "accepted"


def test_update_lesson_missing_id_returns_none(tmp_path):
    semantic = _semantic(tmp_path)
    render_lessons.append_lesson(_lesson("lesson_a", "some claim"), semantic)
    assert render_lessons.update_lesson("lesson_nope", semantic, status="superseded") is None
    # File unchanged.
    assert len(_rows(semantic)) == 1


def test_update_lesson_updates_last_matching_row(tmp_path):
    semantic = _semantic(tmp_path)
    render_lessons.append_lesson(_lesson("lesson_dup", "first version"), semantic)
    render_lessons.append_lesson(_lesson("lesson_dup", "second version"), semantic)
    updated = render_lessons.update_lesson("lesson_dup", semantic, status="superseded")
    assert updated["claim"] == "second version"
    rows = _rows(semantic)
    assert rows[0]["status"] == "accepted"
    assert rows[1]["status"] == "superseded"


def test_update_lesson_missing_file_returns_none(tmp_path):
    semantic = _semantic(tmp_path)
    assert render_lessons.update_lesson("lesson_x", semantic, status="superseded") is None


def test_persisted_superseded_by_renders_strikethrough(tmp_path):
    """Persisted superseded_by on the old row is honored even without the
    new row's supersedes pointer (the inverse map)."""
    semantic = _semantic(tmp_path)
    render_lessons.append_lesson(_lesson("lesson_old", "use the old way"), semantic)
    render_lessons.update_lesson(
        "lesson_old", semantic,
        status="superseded", superseded_by="lesson_new",
    )
    render_lessons.render_lessons(semantic)
    md = (Path(semantic) / "LESSONS.md").read_text()
    assert "~~use the old way~~" in md
    assert "superseded_by=lesson_new" in md


def test_missing_temporal_fields_still_render(tmp_path):
    """Pre-Phase-2 rows (no valid_from / superseded_by) render unchanged."""
    semantic = _semantic(tmp_path)
    render_lessons.append_lesson(_lesson("lesson_plain", "plain lesson"), semantic)
    render_lessons.render_lessons(semantic)
    md = (Path(semantic) / "LESSONS.md").read_text()
    assert "- plain lesson" in md
    assert "~~" not in md
    assert "superseded_by=" not in md


def test_extract_lesson_lines_skips_superseded_status(tmp_path):
    """A persisted status=superseded row is terminal for duplicate detection
    (validate.extract_lesson_lines reads the status annotation)."""
    import validate

    semantic = _semantic(tmp_path)
    render_lessons.append_lesson(_lesson("lesson_old", "use the old way"), semantic)
    render_lessons.append_lesson(_lesson("lesson_new", "use the new way"), semantic)
    render_lessons.update_lesson(
        "lesson_old", semantic,
        status="superseded", superseded_by="lesson_new",
    )
    render_lessons.render_lessons(semantic)
    md = (Path(semantic) / "LESSONS.md").read_text()
    lines = validate.extract_lesson_lines(md)
    assert "use the new way" in lines
    assert "use the old way" not in lines
