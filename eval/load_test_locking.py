#!/usr/bin/env python3
"""Stress test for runtime.core.locking — the load-bearing primitive
behind every events.log.jsonl append.

Why this matters:
- The auto-recall hook fires on every UserPromptSubmit. In a busy session
  with concurrent tool calls + multi-session work, multiple processes can
  append to the same `events.log.jsonl` simultaneously.
- The flock-on-sentinel pattern (lessons memory: feedback_4299f14bdaeb)
  is correct in design. This test is: under 100 concurrent appenders,
  do we actually get exactly 100 well-formed JSONL lines, no corruption,
  no losses?
- A failure here would be an invariant violation, not a perf issue.
  We're testing for races that would silently lose telemetry, not speed.

Usage:
    eval/load_test_locking.py [--n 100] [--workers 100]

Test design:
- Spin up N independent worker processes (multiprocessing, not threads —
  honest concurrency, no GIL serialization).
- Each worker calls locked_append() with a unique payload that includes
  its worker_id and a sequence number.
- Verify: line count == N, every line is valid JSON, every worker_id
  appears exactly once, no truncated lines.

Failure modes we're hunting:
1. Lost lines (line count < N): flock didn't serialize a race.
2. Corrupt lines (JSON decode error): partial writes interleaved.
3. Duplicate or missing worker_ids: lock semantics broken.
4. Timing anomalies (test takes minutes instead of seconds): contention.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from runtime.core.locking import locked_append  # noqa: E402


def _worker(args):
    """Append one line under the lock. Run in subprocess pool."""
    log_path, worker_id = args
    payload = {
        "event": "LoadTest",
        "worker_id": worker_id,
        "pid": os.getpid(),
        "ts_ns": time.monotonic_ns(),
    }
    locked_append(log_path, json.dumps(payload))
    return worker_id


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--n", type=int, default=100,
                   help="number of concurrent appends (default: 100)")
    p.add_argument("--workers", type=int, default=None,
                   help="pool size (default: same as --n for max contention)")
    args = p.parse_args(argv)

    workers = args.workers or args.n

    with tempfile.TemporaryDirectory(prefix="brainstack-loadtest-") as tmpdir:
        log_path = Path(tmpdir) / "events.log.jsonl"
        print(f"target: {log_path}")
        print(f"firing {args.n} concurrent appends across {workers} workers...")
        t0 = time.perf_counter()

        with mp.Pool(workers) as pool:
            pool.map(_worker, [(str(log_path), i) for i in range(args.n)])

        wall_ms = int((time.perf_counter() - t0) * 1000)
        print(f"wall time: {wall_ms}ms ({args.n / (wall_ms / 1000):.0f} appends/sec)")

        text = log_path.read_text(encoding="utf-8")
        lines = [ln for ln in text.split("\n") if ln]
        print(f"\nlines on disk: {len(lines)}")

        ok = True

        if len(lines) != args.n:
            print(f"  FAIL: expected {args.n} lines, got {len(lines)}")
            ok = False
        else:
            print(f"  OK  : line count matches input ({args.n})")

        worker_ids: set[int] = set()
        bad_lines = 0
        for i, line in enumerate(lines):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"  FAIL: line {i} is not valid JSON: {exc}")
                print(f"        first 100 chars: {line[:100]!r}")
                bad_lines += 1
                ok = False
                continue
            wid = rec.get("worker_id")
            if not isinstance(wid, int):
                print(f"  FAIL: line {i} missing worker_id")
                ok = False
                continue
            if wid in worker_ids:
                print(f"  FAIL: worker_id {wid} appears twice")
                ok = False
            worker_ids.add(wid)

        if bad_lines == 0:
            print("  OK  : every line is valid JSON")

        expected_ids = set(range(args.n))
        missing = expected_ids - worker_ids
        if missing:
            print(f"  FAIL: missing worker_ids: {sorted(missing)[:10]}{'...' if len(missing) > 10 else ''}")
            ok = False
        else:
            print("  OK  : every worker_id present (no losses)")

        extra = worker_ids - expected_ids
        if extra:
            print(f"  FAIL: unexpected worker_ids: {sorted(extra)[:10]}")
            ok = False

        if ok:
            print(f"\n  PASS  ✅  locked_append survived {args.n} concurrent appenders")
            return 0
        else:
            print("\n  FAIL  ❌  locking primitive has a race")
            return 1


if __name__ == "__main__":
    sys.exit(main())
