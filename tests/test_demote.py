"""End-to-end tests for demote.py (the inverse of graduate.py).

Demote walks back a previously-graduated lesson: removes the row from
semantic/lessons.jsonl, re-renders LESSONS.md, and moves the candidate
file from candidates/graduated/ to candidates/rejected/ with a `demoted`
decision attached.

Companion to test_namespaced_tools.py — that file exercises the
graduate path; this one exercises the inverse so a regression in either
direction is caught.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "agent" / "tools"


def _seed_candidate(brain: Path, namespace: str, candidate_id: str,
                    claim: str) -> Path:
    """Mirror test_namespaced_tools._seed_candidate so demote and graduate
    are exercised against the same on-disk shape."""
    cdir = (brain / "memory" / "candidates" if namespace == "default"
            else brain / "memory" / "candidates" / namespace)
    cdir.mkdir(parents=True, exist_ok=True)
    cand = {
        "id": candidate_id,
        "key": candidate_id,
        "name": candidate_id,
        "claim": claim,
        "conditions": [],
        "evidence_ids": [],
        "cluster_size": 1,
        "canonical_salience": 7.5,
        "staged_at": "2026-04-26T00:00:00+00:00",
        "status": "staged",
        "decisions": [],
        "rejection_count": 0,
    }
    path = cdir / f"{candidate_id}.json"
    path.write_text(json.dumps(cand))
    return path


def _run(args, brain):
    env = os.environ.copy()
    env["BRAIN_ROOT"] = str(brain)
    return subprocess.run(
        [sys.executable] + args,
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )


def _graduate_then(brain: Path, namespace: str, cid: str,
                   claim: str) -> None:
    """Helper: stage + graduate a candidate so the demote test has a
    realistic graduated artifact to walk back. Failures bubble up with
    full subprocess output."""
    _seed_candidate(brain, namespace, cid, claim)
    args = [str(TOOLS / "graduate.py"), cid,
            "--rationale", "seeded for demote test"]
    if namespace != "default":
        args += ["--namespace", namespace]
    res = _run(args, brain)
    assert res.returncode == 0, (
        f"seed graduation failed:\nstdout: {res.stdout}\n"
        f"stderr: {res.stderr}"
    )


@pytest.fixture
def brain(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path))
    return tmp_path


def test_demote_happy_path_default_namespace(brain):
    """Graduate a lesson, then demote it. Assert:
      - candidate moved from graduated/ to rejected/
      - lessons.jsonl row removed
      - LESSONS.md no longer contains the claim
      - rejected candidate has a `demoted` decision with the reason"""
    claim = "Stale lesson: this looked durable but turned out to be noise"
    _graduate_then(brain, "default", "cand_demote_1", claim)

    # Pre-condition sanity: lesson is present.
    lessons_jsonl = brain / "memory" / "semantic" / "lessons.jsonl"
    rows = [json.loads(l) for l in lessons_jsonl.read_text().splitlines()
            if l.strip()]
    assert any(r["claim"] == claim for r in rows)

    res = _run([
        str(TOOLS / "demote.py"), "cand_demote_1",
        "--reason", "graduated before filter shipped; matches activity_log:edited",
    ], brain)
    assert res.returncode == 0, (
        f"stdout: {res.stdout}\nstderr: {res.stderr}"
    )

    # Candidate file moved graduated/ -> rejected/.
    grad = brain / "memory" / "candidates" / "graduated" / "cand_demote_1.json"
    rej = brain / "memory" / "candidates" / "rejected" / "cand_demote_1.json"
    assert not grad.exists(), "graduated file should be moved"
    assert rej.exists(), "rejected file should exist"

    # Lesson row gone from jsonl.
    rows_after = [json.loads(l) for l in lessons_jsonl.read_text().splitlines()
                  if l.strip()]
    assert not any(r["claim"] == claim for r in rows_after)

    # LESSONS.md no longer references the claim.
    lessons_md = (brain / "memory" / "semantic" / "LESSONS.md").read_text()
    assert claim not in lessons_md

    # Rejected candidate has a `demoted` decision.
    cand = json.loads(rej.read_text())
    assert cand["status"] == "rejected"
    assert cand["rejection_count"] == 1
    actions = [d.get("action") for d in cand.get("decisions", [])]
    assert "demoted" in actions
    demoted = next(d for d in cand["decisions"] if d.get("action") == "demoted")
    assert "filter" in (demoted.get("notes") or "").lower()


def test_demote_requires_reason(brain):
    """Same contract as graduate.py / reject.py — empty or missing
    reason is rejected at the argparse level so an unreviewed demote
    can't slip through."""
    _graduate_then(brain, "default", "cand_demote_2",
                   "A reasonably long lesson statement that passes "
                   "the graduation heuristic check for content words")
    res = _run([str(TOOLS / "demote.py"), "cand_demote_2"], brain)
    assert res.returncode != 0
    assert "--reason" in res.stderr or "required" in res.stderr.lower()


def test_demote_unknown_candidate_errors(brain):
    """No graduated/<cid>.json on disk → exit 1, no side effects."""
    res = _run([
        str(TOOLS / "demote.py"), "does_not_exist",
        "--reason", "test",
    ], brain)
    assert res.returncode != 0
    assert "not found" in (res.stderr + res.stdout).lower()


def test_demote_namespace_routes_to_inbox(brain):
    """--namespace inbox demotes within the inbox brain only; the
    default lessons.jsonl must not appear."""
    claim = ("An inbox-only lesson to walk back that has enough words "
             "to pass the graduation heuristic content check")
    _graduate_then(brain, "inbox", "cand_demote_ns1", claim)
    res = _run([
        str(TOOLS / "demote.py"), "cand_demote_ns1",
        "--reason", "test demote in namespaced brain",
        "--namespace", "inbox",
    ], brain)
    assert res.returncode == 0, (
        f"stdout: {res.stdout}\nstderr: {res.stderr}"
    )
    rej = (brain / "memory" / "candidates" / "inbox" / "rejected"
           / "cand_demote_ns1.json")
    assert rej.exists()
    default_lessons = brain / "memory" / "semantic" / "lessons.jsonl"
    assert not default_lessons.exists()


def test_demote_idempotent_when_lesson_already_gone(brain):
    """If the candidate is in graduated/ but the lesson row is missing
    from jsonl (mid-crash retry, or a hand-edited brain), demote should
    still succeed and move the candidate. remove_lesson is a no-op in
    this case; the demote prints a `note:` and proceeds.

    Without this, an interrupted demote leaves the candidate stranded
    in graduated/ forever."""
    claim = ("Lesson with semantic row pre-emptively removed during "
             "a crash recovery scenario that demote handles gracefully")
    _graduate_then(brain, "default", "cand_demote_3", claim)

    # Manually strip the lesson row, simulating mid-crash recovery.
    lessons_jsonl = brain / "memory" / "semantic" / "lessons.jsonl"
    rows = [json.loads(l) for l in lessons_jsonl.read_text().splitlines()
            if l.strip()]
    rows = [r for r in rows if r["claim"] != claim]
    lessons_jsonl.write_text("".join(json.dumps(r) + "\n" for r in rows))

    res = _run([
        str(TOOLS / "demote.py"), "cand_demote_3",
        "--reason", "complete an interrupted prior demote",
    ], brain)
    assert res.returncode == 0, (
        f"stdout: {res.stdout}\nstderr: {res.stderr}"
    )
    assert "note: lesson" in res.stderr  # surfaced to the operator
    rej = (brain / "memory" / "candidates" / "rejected"
           / "cand_demote_3.json")
    assert rej.exists()
