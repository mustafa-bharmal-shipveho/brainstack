"""Namespaced concurrent-append stress test for v0.2-rc1.

Mirrors test_concurrent_appends.py but spreads writes across two
namespaces (default + inbox). Verifies:
  - sentinel locks are per-file (so the two namespaces don't contend)
  - 800 total rows split 50/50 → 0 lost in either namespace
  - No cross-contamination between the two episodic files
  - Re-runs 3 times to surface flakes
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


def _appender_worker(jsonl_path: str, hooks_dir: str, worker_id: int,
                     n_rows: int, tag: str):
    """Append n_rows JSONL lines via the same path the post-tool hook uses."""
    sys.path.insert(0, hooks_dir)
    import _episodic_io
    for i in range(n_rows):
        entry = {
            "id": f"{tag}-{worker_id:02d}-{i}",
            "salience": 5,
            "summary": f"row from {tag} worker {worker_id} step {i}",
        }
        _episodic_io.append_jsonl(jsonl_path, entry)
        time.sleep(0.001)


def _count_ids_with_prefix(jsonl_path: Path, prefix: str):
    if not jsonl_path.exists():
        return set()
    ids = set()
    for line in jsonl_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("id"), str) \
                and obj["id"].startswith(prefix):
            ids.add(obj["id"])
    return ids


def _run_one_round(tmp_path):
    """One full round: 20 workers × 20 rows × 2 namespaces = 800 writes."""
    brain = tmp_path / ".agent"
    (brain / "memory" / "episodic").mkdir(parents=True)
    (brain / "memory" / "episodic" / "inbox").mkdir(parents=True)

    default_jsonl = brain / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl"
    inbox_jsonl = brain / "memory" / "episodic" / "inbox" / "AGENT_LEARNINGS.jsonl"
    default_jsonl.touch()
    inbox_jsonl.touch()

    hooks_dir = str(REPO_ROOT / "agent" / "harness" / "hooks")

    n_workers = 10  # per namespace → 20 total ("20-way")
    rows_per_worker = 40  # 10 × 40 × 2 namespaces = 800 total writes

    expected_default = {f"def-{w:02d}-{i}"
                        for w in range(n_workers) for i in range(rows_per_worker)}
    expected_inbox = {f"inb-{w:02d}-{i}"
                      for w in range(n_workers) for i in range(rows_per_worker)}

    ctx = mp.get_context("spawn")
    procs = []
    for w in range(n_workers):
        procs.append(ctx.Process(target=_appender_worker,
                                 args=(str(default_jsonl), hooks_dir, w,
                                       rows_per_worker, "def")))
        procs.append(ctx.Process(target=_appender_worker,
                                 args=(str(inbox_jsonl), hooks_dir, w,
                                       rows_per_worker, "inb")))

    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)

    surviving_default = _count_ids_with_prefix(default_jsonl, "def-")
    surviving_inbox = _count_ids_with_prefix(inbox_jsonl, "inb-")

    # Cross-contamination: inbox file should never carry default-prefixed IDs
    # and vice versa.
    cross1 = _count_ids_with_prefix(default_jsonl, "inb-")
    cross2 = _count_ids_with_prefix(inbox_jsonl, "def-")

    return {
        "default_lost": expected_default - surviving_default,
        "inbox_lost": expected_inbox - surviving_inbox,
        "cross_default_to_inbox": cross1,
        "cross_inbox_to_default": cross2,
        "total_expected": len(expected_default) + len(expected_inbox),
    }


@pytest.mark.timeout(120)
@pytest.mark.parametrize("round_idx", [0, 1, 2])
def test_namespaced_concurrent_appends_lose_no_rows(tmp_path, round_idx):
    """Re-run 3 times to catch flakes (parametrized so each run is isolated)."""
    sub = tmp_path / f"round_{round_idx}"
    sub.mkdir()
    summary = _run_one_round(sub)

    assert summary["total_expected"] == 800, (
        f"sanity check: expected 800 total writes, got {summary['total_expected']}"
    )
    assert not summary["default_lost"], (
        f"default ns: lost {len(summary['default_lost'])} rows: "
        f"{sorted(list(summary['default_lost']))[:10]}"
    )
    assert not summary["inbox_lost"], (
        f"inbox ns: lost {len(summary['inbox_lost'])} rows: "
        f"{sorted(list(summary['inbox_lost']))[:10]}"
    )
    assert not summary["cross_default_to_inbox"], (
        "cross-contamination: inbox-prefixed IDs landed in default file"
    )
    assert not summary["cross_inbox_to_default"], (
        "cross-contamination: default-prefixed IDs landed in inbox file"
    )
