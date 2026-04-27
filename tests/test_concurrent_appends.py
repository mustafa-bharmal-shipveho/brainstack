"""Stress test for the dream cycle ↔ appender concurrency.

The previous implementation locked the data file (`AGENT_LEARNINGS.jsonl`)
directly. After the dream cycle's `os.replace` swapped the data file's
inode, appenders that had opened the path BEFORE the replace ended up
holding flock on the orphan inode and writing bytes that were unreachable
from the path — a silent ~3% data loss under contention.

The fix locks a sentinel sibling file. This test verifies that:
  - 20 concurrent appenders + 1 dream cycle → no rows lost
  - The lock identity survives os.replace
"""
import json
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Tests need fcntl (POSIX). Skip on Windows.
fcntl = pytest.importorskip("fcntl")


def _setup_brain(brain: Path) -> None:
    """Build a brain layout sufficient for auto_dream.run_dream_cycle()."""
    (brain / "memory" / "episodic").mkdir(parents=True)
    (brain / "memory" / "episodic" / "snapshots").mkdir()
    (brain / "memory" / "working").mkdir()
    (brain / "memory" / "candidates").mkdir()
    (brain / "memory" / "semantic" / "lessons").mkdir(parents=True)
    (brain / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl").touch()

    src_mem = REPO_ROOT / "agent" / "memory"
    for f in src_mem.iterdir():
        if f.is_file() and f.suffix == ".py":
            (brain / "memory" / f.name).write_text(f.read_text())

    src_harness = REPO_ROOT / "agent" / "harness"
    (brain / "harness").mkdir()
    for f in ("text.py", "salience.py"):
        (brain / "harness" / f).write_text((src_harness / f).read_text())
    (brain / "harness" / "hooks").mkdir()
    for f in (REPO_ROOT / "agent" / "harness" / "hooks").iterdir():
        if f.is_file() and f.suffix == ".py":
            (brain / "harness" / "hooks" / f.name).write_text(f.read_text())


def _appender_worker(jsonl_path: str, hooks_dir: str, worker_id: int, n_rows: int):
    """Append n_rows JSONL lines via the same path the post-tool hook uses."""
    sys.path.insert(0, hooks_dir)
    import _episodic_io
    for i in range(n_rows):
        entry = {
            "id": f"app-{worker_id:02d}-{i}",
            "salience": 5,
            "summary": f"appended row from worker {worker_id} step {i}",
            "claim": f"worker {worker_id} step {i}",
        }
        _episodic_io.append_jsonl(jsonl_path, entry)
        # Tiny stagger so writes interleave with the dream cycle
        time.sleep(0.001)


def _dream_worker(brain: str, sleep_before: float):
    """Run the dream cycle inside a child process. `sleep_before` lets us
    align the cycle with the appender wave."""
    import sys
    sys.path.insert(0, str(Path(brain) / "memory"))
    sys.path.insert(0, str(Path(brain) / "harness"))
    time.sleep(sleep_before)
    import auto_dream
    auto_dream.EPISODIC = str(Path(brain) / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl")
    auto_dream.EPISODIC_LOCK = auto_dream.EPISODIC + ".lock"
    auto_dream.ROOT = str(Path(brain) / "memory")
    auto_dream.CANDIDATES = str(Path(brain) / "memory" / "candidates")
    auto_dream.SEMANTIC = str(Path(brain) / "memory" / "semantic")
    auto_dream.REVIEW_QUEUE = str(Path(brain) / "memory" / "working" / "REVIEW_QUEUE.md")
    auto_dream.run_dream_cycle()


def _count_rows_with_id(jsonl_path: Path, prefix: str) -> set[str]:
    """Return the set of `id` values whose ID starts with `prefix`."""
    if not jsonl_path.exists():
        return set()
    ids: set[str] = set()
    for line in jsonl_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("id"), str) and obj["id"].startswith(prefix):
            ids.add(obj["id"])
    return ids


@pytest.mark.timeout(30)
def test_concurrent_appends_with_dream_cycle_lose_no_rows(tmp_path):
    """20 appenders × 5 rows + 1 dream cycle → all 100 rows survive in either
    AGENT_LEARNINGS.jsonl or its snapshots/."""
    brain = tmp_path / ".agent"
    _setup_brain(brain)

    jsonl = brain / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl"
    hooks_dir = str(brain / "harness" / "hooks")

    n_workers = 20
    rows_per_worker = 5
    expected = {f"app-{w:02d}-{i}" for w in range(n_workers) for i in range(rows_per_worker)}

    ctx = mp.get_context("spawn")
    procs = []
    for w in range(n_workers):
        p = ctx.Process(target=_appender_worker, args=(str(jsonl), hooks_dir, w, rows_per_worker))
        procs.append(p)

    dream_p = ctx.Process(target=_dream_worker, args=(str(brain), 0.05))

    for p in procs:
        p.start()
    dream_p.start()

    for p in procs:
        p.join(timeout=20)
    dream_p.join(timeout=20)

    # Check both the live file AND the dream-cycle snapshots/ — both are
    # "the row survived" outcomes (decay archives some old entries).
    live = _count_rows_with_id(jsonl, "app-")
    archived: set[str] = set()
    snap_dir = brain / "memory" / "episodic" / "snapshots"
    if snap_dir.exists():
        for snap in snap_dir.rglob("*.jsonl"):
            archived |= _count_rows_with_id(snap, "app-")

    surviving = live | archived
    lost = expected - surviving
    assert not lost, (
        f"data loss: {len(lost)}/{len(expected)} appended rows lost. "
        f"sample: {sorted(list(lost))[:10]}"
    )


def test_episodic_io_locks_sentinel_not_data_file(tmp_path):
    """Verify the lock identity has been moved off the data file.

    The bug fix's correctness hinges on this: if append_jsonl ever locks
    the data file again, os.replace will resume invalidating in-flight
    appenders' locks.
    """
    sys.path.insert(0, str(REPO_ROOT / "agent" / "harness" / "hooks"))
    import _episodic_io

    src = (REPO_ROOT / "agent" / "harness" / "hooks" / "_episodic_io.py").read_text()
    # A sentinel-locking implementation must NOT call flock on an fd opened
    # against the data file directly.
    assert "_sentinel_path" in src, "expected a sentinel path helper"
    # The flock call should target the sentinel fd, not f.fileno()
    assert "fcntl.flock(lock_fd" in src, (
        "appender should flock the sentinel descriptor (lock_fd), "
        "not the data-file fd"
    )
    # The old broken pattern — flock on the data-file's fileno() — must
    # not appear.
    assert "fcntl.flock(f.fileno()" not in src, (
        "appender still locks the data file directly; os.replace will "
        "invalidate the lock"
    )
