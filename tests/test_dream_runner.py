"""Tests for tools/dream_runner.py — fcntl-based dream cycle launcher.

Validates:
  - exits 0 + runs the cycle on an empty brain
  - exits 0 (skip) when another process holds the brain lock
  - exits 2 when BRAIN_ROOT doesn't exist
  - does NOT depend on the shell `flock(1)` binary
"""
import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNER = REPO_ROOT / "agent" / "tools" / "dream_runner.py"


def make_brain(brain: Path) -> None:
    (brain / "memory" / "episodic").mkdir(parents=True)
    (brain / "memory" / "working").mkdir()
    (brain / "memory" / "candidates").mkdir()
    (brain / "memory" / "semantic" / "lessons").mkdir(parents=True)
    (brain / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl").touch()
    (brain / "harness").mkdir()

    src_mem = REPO_ROOT / "agent" / "memory"
    for f in src_mem.iterdir():
        if f.is_file() and f.suffix == ".py":
            (brain / "memory" / f.name).write_text(f.read_text())
    src_harness = REPO_ROOT / "agent" / "harness"
    for f in ("text.py", "salience.py"):
        (brain / "harness" / f).write_text((src_harness / f).read_text())


def test_empty_brain_runs_clean(tmp_path):
    brain = tmp_path / ".agent"
    make_brain(brain)
    r = subprocess.run(
        [sys.executable, str(RUNNER), "--brain-root", str(brain)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"stderr: {r.stderr}"
    assert "no entries" in r.stdout or "dream cycle" in r.stdout


def test_missing_brain_returns_2(tmp_path):
    r = subprocess.run(
        [sys.executable, str(RUNNER), "--brain-root", str(tmp_path / "nonexistent")],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "not found" in r.stderr.lower()


def test_lock_contention_exits_zero(tmp_path):
    """Hold the brain lock externally; the runner should exit 0 quietly."""
    brain = tmp_path / ".agent"
    make_brain(brain)
    lock_path = brain / ".brain.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        r = subprocess.run(
            [sys.executable, str(RUNNER), "--brain-root", str(brain)],
            capture_output=True, text=True, timeout=10,
        )
        assert r.returncode == 0
        assert "in progress" in r.stderr.lower() or "skipping" in r.stderr.lower()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_runner_does_not_call_shell_flock():
    """The runner should not shell out to flock(1) — Python fcntl only."""
    text = RUNNER.read_text()
    # No `subprocess` invocation of flock
    assert "flock(1)" not in text or "subprocess" not in text or "flock" not in text.split("subprocess")[1]
    # Must use fcntl
    assert "import fcntl" in text
    assert "fcntl.flock" in text
