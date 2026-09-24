"""Dream-cycle contradiction detection stages proposals but NEVER graduates.

Boundary under test: an unattended `auto_dream.run()` may add
kind="supersession" candidates to the review queue, but lessons.jsonl and
LESSONS.md must be byte-identical before and after — applying a
supersession is graduate.py's job, gated on a human decision.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MEMORY_DIR = REPO_ROOT / "agent" / "memory"
HARNESS_DIR = REPO_ROOT / "agent" / "harness"

# agent/memory MUST win over agent/tools: both ship a `promote.py` (see
# tests/test_dream_status.py for the full explanation of this dance).
for _d in (HARNESS_DIR, MEMORY_DIR):
    while str(_d) in sys.path:
        sys.path.remove(str(_d))
    sys.path.insert(0, str(_d))
_stale = sys.modules.get("promote")
if _stale is not None and "agent/tools" in (getattr(_stale, "__file__", "") or ""):
    del sys.modules["promote"]

pytest.importorskip("fcntl")

import render_lessons  # noqa: E402

OLD_CLAIM = "Always run the full migration suite before any prod deploy"
NEW_CLAIM = "Skip the migration suite entirely for fast prod deploys"
CONDITIONS = ["deploy", "migration", "prod"]


@pytest.fixture
def auto_dream():
    stale = sys.modules.get("promote")
    if stale is not None and "agent/tools" in (getattr(stale, "__file__", "") or ""):
        del sys.modules["promote"]
        sys.modules.pop("auto_dream", None)
    return __import__("auto_dream")


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return home


def _make_brain(tmp_path: Path, monkeypatch) -> Path:
    brain = tmp_path / ".agent"
    (brain / "memory" / "episodic" / "snapshots").mkdir(parents=True)
    (brain / "memory" / "working").mkdir(parents=True)
    (brain / "memory" / "candidates").mkdir(parents=True)
    (brain / "memory" / "semantic" / "lessons").mkdir(parents=True)
    monkeypatch.setenv("BRAIN_ROOT", str(brain))
    return brain


def _seed_lesson(brain: Path) -> None:
    semantic = brain / "memory" / "semantic"
    render_lessons.append_lesson({
        "id": "lesson_old",
        "claim": OLD_CLAIM,
        "conditions": CONDITIONS,
        "evidence_ids": ["2026-01-01T00:00:00Z"],
        "status": "accepted",
        "accepted_at": "2026-01-01T00:00:00Z",
        "reviewer": "host-agent",
        "rationale": "seeded",
        "supersedes": None,
    }, str(semantic))
    render_lessons.render_lessons(str(semantic))


def _seed_contradicting_candidate(brain: Path) -> None:
    cand = {
        "id": "cand_new",
        "key": "cand_new",
        "name": "cand_new",
        "claim": NEW_CLAIM,
        "conditions": CONDITIONS,
        "evidence_ids": ["2026-06-01T00:00:00Z"],
        "cluster_size": 3,
        "canonical_salience": 8.0,
        "staged_at": "2026-06-01T00:00:00+00:00",
        "status": "staged",
        "decisions": [{"ts": "2026-06-01T00:00:00+00:00", "action": "staged",
                       "reviewer": "auto_dream"}],
        "rejection_count": 0,
    }
    (brain / "memory" / "candidates" / "cand_new.json").write_text(json.dumps(cand))


def _seed_episodic(brain: Path) -> None:
    entry = {
        "id": "e-1",
        "timestamp": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        "action": "ran deploy",
        "detail": "deploy detail",
        "result": "success",
    }
    (brain / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl").write_text(
        json.dumps(entry) + "\n")


def _supersession_candidates(brain: Path) -> list[dict]:
    out = []
    for p in (brain / "memory" / "candidates").glob("*.json"):
        data = json.loads(p.read_text())
        if data.get("kind") == "supersession":
            out.append(data)
    return out


def test_dream_stages_but_does_not_graduate(tmp_path, isolated_home, monkeypatch,
                                            auto_dream):
    brain = _make_brain(tmp_path, monkeypatch)
    _seed_lesson(brain)
    _seed_contradicting_candidate(brain)
    _seed_episodic(brain)

    jsonl = brain / "memory" / "semantic" / "lessons.jsonl"
    md = brain / "memory" / "semantic" / "LESSONS.md"
    before_jsonl, before_md = jsonl.read_bytes(), md.read_bytes()

    result = auto_dream.run(brain_root=str(brain), namespace="default",
                            dry_run=False)

    # A supersession proposal was STAGED on the review queue…
    staged = _supersession_candidates(brain)
    assert len(staged) == 1, f"expected 1 staged proposal, got {staged}"
    assert staged[0]["supersedes"] == "lesson_old"
    assert staged[0]["claim"] == NEW_CLAIM
    assert staged[0]["status"] == "staged"
    assert result.get("supersession_proposals") == 1

    # …but durable memory is UNTOUCHED: no auto-graduation, no status flip.
    assert jsonl.read_bytes() == before_jsonl
    assert md.read_bytes() == before_md


def test_dream_dry_run_writes_nothing(tmp_path, isolated_home, monkeypatch,
                                      auto_dream):
    brain = _make_brain(tmp_path, monkeypatch)
    _seed_lesson(brain)
    _seed_contradicting_candidate(brain)
    _seed_episodic(brain)

    jsonl = brain / "memory" / "semantic" / "lessons.jsonl"
    before_jsonl = jsonl.read_bytes()
    before_candidates = sorted(p.name for p in (brain / "memory" / "candidates").glob("*.json"))

    result = auto_dream.run(brain_root=str(brain), namespace="default",
                            dry_run=True)

    # Reported, not written.
    assert result.get("supersession_proposals", 0) >= 1
    assert jsonl.read_bytes() == before_jsonl
    after_candidates = sorted(p.name for p in (brain / "memory" / "candidates").glob("*.json"))
    assert after_candidates == before_candidates
    assert not (brain / "memory" / "candidates" / "rejected").exists()
