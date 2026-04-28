"""End-to-end tests for the v0.2-rc1 namespaced CLI tools.

Covers:
  - graduate.py --namespace inbox: candidates/inbox → semantic/inbox
  - graduate.py without flag: still works on default at v0.1 paths
  - reject.py --namespace inbox
  - promote.py / rollback.py round-trip via policy file
  - audit log entries written under memory/audit/<ns>/
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "agent" / "tools"


def _seed_candidate(brain, namespace, candidate_id, claim):
    if namespace == "default":
        cdir = brain / "memory" / "candidates"
    else:
        cdir = brain / "memory" / "candidates" / namespace
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


@pytest.fixture
def brain(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path))
    return tmp_path


def test_graduate_namespace_inbox_routes_to_inbox_paths(brain):
    claim = "Always set BRAIN_ROOT to validate hook script presence in v0.2"
    _seed_candidate(brain, "inbox", "cand_inbox_1", claim)

    res = _run([
        str(TOOLS / "graduate.py"), "cand_inbox_1",
        "--rationale", "verified by reviewer in CI run",
        "--namespace", "inbox",
    ], brain)
    assert res.returncode == 0, f"stderr: {res.stderr}\nstdout: {res.stdout}"

    # Lesson landed in inbox-namespaced semantic dir.
    lessons_jsonl = brain / "memory" / "semantic" / "inbox" / "lessons.jsonl"
    assert lessons_jsonl.exists(), \
        f"expected {lessons_jsonl}\nstdout: {res.stdout}\nstderr: {res.stderr}"
    rows = [json.loads(l) for l in lessons_jsonl.read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["claim"] == claim

    # Candidate moved to inbox-namespaced graduated dir.
    grad = brain / "memory" / "candidates" / "inbox" / "graduated" / "cand_inbox_1.json"
    assert grad.exists()

    # Default-namespace lessons.jsonl should NOT have been touched.
    default_lessons = brain / "memory" / "semantic" / "lessons.jsonl"
    assert not default_lessons.exists()


def test_graduate_without_flag_still_works_on_default(brain):
    """Backward compat: no --namespace flag → v0.1 layout (top-level paths)."""
    claim = "Default namespace candidates still graduate without explicit flag"
    _seed_candidate(brain, "default", "cand_default_1", claim)

    res = _run([
        str(TOOLS / "graduate.py"), "cand_default_1",
        "--rationale", "compat test",
    ], brain)
    assert res.returncode == 0, f"stderr: {res.stderr}\nstdout: {res.stdout}"

    lessons_jsonl = brain / "memory" / "semantic" / "lessons.jsonl"
    assert lessons_jsonl.exists()
    # And the namespaced path should NOT exist under "default".
    assert not (brain / "memory" / "semantic" / "default" / "lessons.jsonl").exists()


def test_reject_namespace_inbox(brain):
    _seed_candidate(brain, "inbox", "cand_inbox_r", "claim that we will reject")
    res = _run([
        str(TOOLS / "reject.py"), "cand_inbox_r",
        "--reason", "not actionable",
        "--namespace", "inbox",
    ], brain)
    assert res.returncode == 0, res.stderr

    rejected = (brain / "memory" / "candidates" / "inbox" / "rejected"
                / "cand_inbox_r.json")
    assert rejected.exists()
    cand = json.loads(rejected.read_text())
    assert cand["status"] == "rejected"
    assert cand["rejection_count"] == 1


def test_promote_writes_policy_and_audit(brain):
    res = _run([
        str(TOOLS / "promote.py"),
        "--namespace", "inbox",
        "--target", "ceo@example.com",
        "--tier", "2",
        "--reason", "verified executive correspondence",
        "--reviewer", "host-agent",
    ], brain)
    assert res.returncode == 0, res.stderr

    policy = brain / "memory" / "semantic" / "inbox" / "policy.yaml"
    policy_json = brain / "memory" / "semantic" / "inbox" / "policy.json"
    assert policy.exists() or policy_json.exists()
    audit = brain / "memory" / "audit" / "inbox" / "promotions.jsonl"
    assert audit.exists()
    rows = [json.loads(l) for l in audit.read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["action"] == "promote"
    assert rows[0]["target"] == "ceo@example.com"
    assert rows[0]["tier"] == 2


def test_promote_then_rollback_round_trip(brain):
    """Promote, then rollback: tier drops to 1 and cooldown is set."""
    _run([
        str(TOOLS / "promote.py"),
        "--namespace", "inbox",
        "--target", "alice@example.com",
        "--tier", "2",
        "--reason", "good signal",
    ], brain)

    res = _run([
        str(TOOLS / "rollback.py"),
        "--namespace", "inbox",
        "--target", "alice@example.com",
        "--reason", "false positives observed",
    ], brain)
    assert res.returncode == 0, res.stderr

    # Read the policy via the SDK to verify final state.
    sys.path.insert(0, str(REPO_ROOT / "agent" / "memory"))
    sys.path.insert(0, str(REPO_ROOT / "agent" / "harness" / "hooks"))
    import sdk  # noqa: E402
    pol = sdk.read_policy("inbox", brain_root=str(brain))
    entry = pol["tiers"]["alice@example.com"]
    assert entry["tier"] == 1
    assert "cooldown_until" in entry
    assert entry["reason"] == "false positives observed"

    # Audit log has 2 rows.
    audit = brain / "memory" / "audit" / "inbox" / "promotions.jsonl"
    rows = [json.loads(l) for l in audit.read_text().splitlines() if l.strip()]
    assert len(rows) == 2
    assert [r["action"] for r in rows] == ["promote", "rollback"]
