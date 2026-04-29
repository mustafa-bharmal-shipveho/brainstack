"""Tests for PR5's `stats` subcommand on `agent.tools.sdk_cli`.

Exercises the wire shape via subprocess so the agentry-side
`BrainstackPythonProvider.stats()` consumer can rely on it.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_cli(brain_root: Path, *extra_args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "agent.tools.sdk_cli", "stats",
         "--brain-root", str(brain_root), *extra_args],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )


def _seed_jsonl(path: Path, lines: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for i in range(lines):
            f.write(json.dumps({"i": i}) + "\n")


def test_stats_empty_brain(tmp_path):
    """Brand-new brain with no episodic dirs yet → all zeros."""
    r = _run_cli(tmp_path)
    assert r.returncode == 0, f"stderr={r.stderr}"
    out = json.loads(r.stdout)
    assert out["episodeCount"] == 0
    assert out["lessonCount"] == 0
    assert out["candidateCount"] == 0
    assert out["namespaces"] == []


def test_stats_aggregates_default_namespace(tmp_path):
    """Default-ns AGENT_LEARNINGS.jsonl reported as namespace 'default'."""
    _seed_jsonl(tmp_path / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl", 5)
    _seed_jsonl(tmp_path / "memory" / "semantic" / "lessons.jsonl", 2)
    r = _run_cli(tmp_path)
    assert r.returncode == 0, f"stderr={r.stderr}"
    out = json.loads(r.stdout)
    assert "default" in out["namespaces"]
    assert out["episodeCount"] == 5
    assert out["lessonCount"] == 2
    assert out["perNamespace"]["default"]["episodes"] == 5
    assert out["perNamespace"]["default"]["lessons"] == 2


def test_stats_walks_multiple_namespaces(tmp_path):
    _seed_jsonl(tmp_path / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl", 3)
    _seed_jsonl(tmp_path / "memory" / "episodic" / "inbox" / "AGENT_LEARNINGS.jsonl", 7)
    _seed_jsonl(tmp_path / "memory" / "episodic" / "mustafa-agent" / "AGENT_LEARNINGS.jsonl", 2)
    r = _run_cli(tmp_path)
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert set(out["namespaces"]) >= {"default", "inbox", "mustafa-agent"}
    assert out["episodeCount"] == 12  # 3 + 7 + 2
    assert out["perNamespace"]["inbox"]["episodes"] == 7
    assert out["perNamespace"]["mustafa-agent"]["episodes"] == 2


def test_stats_restricted_to_one_namespace(tmp_path):
    _seed_jsonl(tmp_path / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl", 3)
    _seed_jsonl(tmp_path / "memory" / "episodic" / "inbox" / "AGENT_LEARNINGS.jsonl", 7)
    r = _run_cli(tmp_path, "--namespace", "inbox")
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["namespaces"] == ["inbox"]
    assert out["episodeCount"] == 7  # default not included


def test_stats_invalid_namespace_returns_3(tmp_path):
    r = _run_cli(tmp_path, "--namespace", "../../etc")
    assert r.returncode == 3
    err = json.loads(r.stderr.strip().splitlines()[0])
    assert err["code"] == "invalid_namespace"


def test_stats_skips_reserved_subdirs(tmp_path):
    """`snapshots/`, `working/`, etc. are not counted as namespaces even
    if an AGENT_LEARNINGS.jsonl somehow lands there."""
    # Real layout: episodic/snapshots/ holds dream-cycle archive shards.
    _seed_jsonl(tmp_path / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl", 1)
    _seed_jsonl(tmp_path / "memory" / "episodic" / "snapshots" / "AGENT_LEARNINGS.jsonl", 99)
    r = _run_cli(tmp_path)
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert "snapshots" not in out["namespaces"]
    # The snapshots count should not bleed into the aggregate.
    assert out["episodeCount"] == 1


def test_stats_counts_staged_candidates(tmp_path):
    cand_dir = tmp_path / "memory" / "candidates"
    cand_dir.mkdir(parents=True)
    (cand_dir / "abc123.json").write_text("{}")
    (cand_dir / "def456.json").write_text("{}")
    # rejected/ + graduated/ don't count as staged candidates.
    (cand_dir / "rejected").mkdir()
    (cand_dir / "rejected" / "rej.json").write_text("{}")
    _seed_jsonl(tmp_path / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl", 1)
    r = _run_cli(tmp_path)
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["candidateCount"] == 2
