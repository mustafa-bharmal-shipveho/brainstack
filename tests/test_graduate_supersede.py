"""Supersession persistence on human accept (Phase 2, step 5).

When a reviewer graduates a candidate with --supersedes <old_id>, the OLD
lessons.jsonl row must flip to status=superseded with superseded_by=<new id>
and valid_until=<accepted_at> — ranking and trace can then see it. The new
row carries supersedes + valid_from. A companion markdown file gives recall
one indexable doc per lesson.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "agent" / "tools"
sys.path.insert(0, str(REPO_ROOT / "agent" / "memory"))

import render_lessons  # noqa: E402

OLD_CLAIM = "Always reboot the frobnicate service before deploying to prod"
NEW_CLAIM = "Never reboot the frobnicate service; use rolling deploys instead"


@pytest.fixture
def brain(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path))
    return tmp_path


def _seed_old_lesson(brain):
    semantic = brain / "memory" / "semantic"
    semantic.mkdir(parents=True, exist_ok=True)
    render_lessons.append_lesson({
        "id": "lesson_old",
        "claim": OLD_CLAIM,
        "conditions": ["deploy"],
        "evidence_ids": ["2026-01-01T00:00:00Z"],
        "status": "accepted",
        "accepted_at": "2026-01-01T00:00:00Z",
        "reviewer": "host-agent",
        "rationale": "seeded",
        "cluster_size": 1,
        "canonical_salience": 5.0,
        "confidence": 0.5,
        "support_count": 0,
        "contradiction_count": 0,
        "supersedes": None,
        "source_candidate": None,
    }, str(semantic))
    render_lessons.render_lessons(str(semantic))


def _seed_candidate(brain, candidate_id="cand_new", claim=NEW_CLAIM, **extra):
    cdir = brain / "memory" / "candidates"
    cdir.mkdir(parents=True, exist_ok=True)
    cand = {
        "id": candidate_id,
        "key": candidate_id,
        "name": candidate_id,
        "claim": claim,
        "conditions": ["deploy"],
        "evidence_ids": ["2026-06-01T00:00:00Z"],
        "cluster_size": 2,
        "canonical_salience": 8.0,
        "staged_at": "2026-06-01T00:00:00+00:00",
        "status": "staged",
        "decisions": [],
        "rejection_count": 0,
    }
    cand.update(extra)
    (cdir / f"{candidate_id}.json").write_text(json.dumps(cand))


def _run_graduate(brain, *args):
    env = os.environ.copy()
    env["BRAIN_ROOT"] = str(brain)
    return subprocess.run(
        [sys.executable, str(TOOLS / "graduate.py"), *args],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )


def _jsonl_rows(brain):
    path = brain / "memory" / "semantic" / "lessons.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_accept_marks_old_lesson_superseded(brain):
    _seed_old_lesson(brain)
    _seed_candidate(brain)
    res = _run_graduate(
        brain, "cand_new",
        "--rationale", "rolling deploys replaced reboots after incident review",
        "--supersedes", "lesson_old",
        "--non-interactive-ack",
    )
    assert res.returncode == 0, f"stderr: {res.stderr}\nstdout: {res.stdout}"

    rows = _jsonl_rows(brain)
    old = [r for r in rows if r["id"] == "lesson_old"]
    new = [r for r in rows if r["id"] == "lesson_cand_new"]
    assert len(old) == 1 and len(new) == 1, "no duplicate rows appended"

    assert old[0]["status"] == "superseded"
    assert old[0]["superseded_by"] == "lesson_cand_new"
    assert old[0]["valid_until"] == new[0]["accepted_at"]

    assert new[0]["supersedes"] == "lesson_old"
    assert new[0]["status"] == "accepted"

    # Rendered LESSONS.md shows the old lesson struck through.
    md = (brain / "memory" / "semantic" / "LESSONS.md").read_text()
    assert f"~~{OLD_CLAIM}~~" in md


def test_new_lesson_stamps_valid_from(brain):
    _seed_candidate(brain)
    res = _run_graduate(
        brain, "cand_new",
        "--rationale", "verified practice worth keeping long term",
        "--non-interactive-ack",
    )
    assert res.returncode == 0, f"stderr: {res.stderr}\nstdout: {res.stdout}"
    (new,) = [r for r in _jsonl_rows(brain) if r["id"] == "lesson_cand_new"]
    assert new["valid_from"] == new["accepted_at"]
    assert new["superseded_by"] is None


def test_candidate_kind_supersession_fills_supersedes_flag(brain):
    """A staged supersession candidate carries its own supersedes pointer;
    the reviewer should not have to retype --supersedes."""
    _seed_old_lesson(brain)
    _seed_candidate(
        brain, kind="supersession", supersedes="lesson_old",
        detection_method="slot-conflict",
    )
    res = _run_graduate(
        brain, "cand_new",
        "--rationale", "accepting the staged supersession proposal",
        "--non-interactive-ack",
    )
    assert res.returncode == 0, f"stderr: {res.stderr}\nstdout: {res.stdout}"
    rows = _jsonl_rows(brain)
    old = [r for r in rows if r["id"] == "lesson_old"][0]
    new = [r for r in rows if r["id"] == "lesson_cand_new"][0]
    assert new["supersedes"] == "lesson_old"
    assert old["status"] == "superseded"
    assert old["superseded_by"] == "lesson_cand_new"


def test_without_ack_off_tty_does_not_mutate(brain):
    _seed_old_lesson(brain)
    _seed_candidate(brain)
    res = _run_graduate(
        brain, "cand_new",
        "--rationale", "rolling deploys replaced reboots after incident review",
        "--supersedes", "lesson_old",
        # no --non-interactive-ack; stdin is a pipe under pytest
    )
    assert res.returncode == 4
    rows = _jsonl_rows(brain)
    assert len(rows) == 1
    assert rows[0]["status"] == "accepted"
    assert "superseded_by" not in rows[0]


def test_companion_markdown_frontmatter(brain):
    _seed_old_lesson(brain)
    _seed_candidate(brain)
    res = _run_graduate(
        brain, "cand_new",
        "--rationale", "rolling deploys replaced reboots after incident review",
        "--supersedes", "lesson_old",
        "--non-interactive-ack",
    )
    assert res.returncode == 0, f"stderr: {res.stderr}\nstdout: {res.stdout}"

    md_path = brain / "memory" / "semantic" / "lessons" / "lesson_cand_new.md"
    assert md_path.exists(), "companion markdown missing — recall cannot index per-lesson docs"

    from recall.frontmatter import parse_path, temporal_meta
    parsed = parse_path(md_path)
    meta = temporal_meta(parsed.frontmatter)
    assert meta.status == "current"  # 'accepted' aliases to current
    assert meta.valid_from is not None
    assert meta.supersedes == "lesson_old"
    assert NEW_CLAIM in parsed.body
