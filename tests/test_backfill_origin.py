"""Tests for tools/backfill_origin.py — one-shot migration helper.

PR1 of the brainstack/agentry integration adds an `origin` field to
episodes. Existing JSONL files (3712 lines on the dev box) have no
`origin`. The backfill helper walks a JSONL, stamps `origin:
"coding.tool_call"` on entries missing it, preserves entries that
already have an explicit origin, and is idempotent (running twice
produces the same output).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "agent" / "tools" / "backfill_origin.py"


def _read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(l) for l in f.read().splitlines() if l.strip()]


def _seed_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _run(brain_root, namespace="default", dry_run=False):
    cmd = [sys.executable, str(TOOL), "--brain-root", str(brain_root)]
    if namespace and namespace != "default":
        cmd += ["--namespace", namespace]
    if dry_run:
        cmd += ["--dry-run"]
    return subprocess.run(cmd, capture_output=True, text=True)


def test_backfill_stamps_origin_on_missing_entries(tmp_path):
    jsonl = tmp_path / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl"
    _seed_jsonl(jsonl, [
        {"timestamp": "2026-01-01T00:00:00Z", "skill": "claude-code", "action": "a"},
        {"timestamp": "2026-01-02T00:00:00Z", "skill": "claude-code", "action": "b"},
    ])
    r = _run(tmp_path)
    assert r.returncode == 0, f"stderr: {r.stderr}"
    rows = _read_jsonl(jsonl)
    assert all(row["origin"] == "coding.tool_call" for row in rows)
    # Other fields preserved.
    assert rows[0]["action"] == "a"
    assert rows[1]["action"] == "b"


def test_backfill_preserves_explicit_origin(tmp_path):
    jsonl = tmp_path / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl"
    _seed_jsonl(jsonl, [
        {"action": "a", "origin": "agentry.inbox.action"},
        {"action": "b"},  # missing → should become coding.tool_call
    ])
    r = _run(tmp_path)
    assert r.returncode == 0, f"stderr: {r.stderr}"
    rows = _read_jsonl(jsonl)
    assert rows[0]["origin"] == "agentry.inbox.action"
    assert rows[1]["origin"] == "coding.tool_call"


def test_backfill_is_idempotent(tmp_path):
    jsonl = tmp_path / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl"
    _seed_jsonl(jsonl, [
        {"action": "a"},
        {"action": "b", "origin": "agentry.x.action"},
    ])
    r1 = _run(tmp_path)
    assert r1.returncode == 0
    after_first = _read_jsonl(jsonl)
    r2 = _run(tmp_path)
    assert r2.returncode == 0
    after_second = _read_jsonl(jsonl)
    assert after_first == after_second


def test_backfill_dry_run_does_not_mutate(tmp_path):
    jsonl = tmp_path / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl"
    seed = [
        {"action": "a"},
        {"action": "b"},
    ]
    _seed_jsonl(jsonl, seed)
    r = _run(tmp_path, dry_run=True)
    assert r.returncode == 0
    rows = _read_jsonl(jsonl)
    # File unchanged.
    assert all("origin" not in row for row in rows)
    # Stdout reports the would-be count.
    assert "2" in r.stdout, f"expected count 2 in dry-run output: {r.stdout!r}"


def test_backfill_namespaced_path(tmp_path):
    jsonl = tmp_path / "memory" / "episodic" / "inbox" / "AGENT_LEARNINGS.jsonl"
    _seed_jsonl(jsonl, [{"action": "a"}])
    r = _run(tmp_path, namespace="inbox")
    assert r.returncode == 0, f"stderr: {r.stderr}"
    rows = _read_jsonl(jsonl)
    assert rows[0]["origin"] == "coding.tool_call"
    # Default-ns file should NOT exist.
    default_path = tmp_path / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl"
    assert not default_path.exists()


def test_backfill_empty_file_no_op(tmp_path):
    jsonl = tmp_path / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.touch()
    r = _run(tmp_path)
    assert r.returncode == 0, f"stderr: {r.stderr}"
    assert _read_jsonl(jsonl) == []


def test_backfill_missing_file_returns_2(tmp_path):
    """No JSONL at all → exit 2 with a clear message."""
    r = _run(tmp_path)
    assert r.returncode == 2
    assert "not found" in r.stderr.lower() or "not found" in r.stdout.lower()


def test_backfill_skips_unparseable_lines(tmp_path):
    """Malformed JSON lines don't abort the run."""
    jsonl = tmp_path / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    with open(jsonl, "w") as f:
        f.write('{"action": "a"}\n')
        f.write("not json at all\n")
        f.write('{"action": "b", "origin": "agentry.x"}\n')
    r = _run(tmp_path)
    assert r.returncode == 0, f"stderr: {r.stderr}"
    rows = _read_jsonl(jsonl)
    # Two parseable rows. Malformed line is dropped.
    assert len(rows) == 2
    assert rows[0]["origin"] == "coding.tool_call"
    assert rows[1]["origin"] == "agentry.x"
