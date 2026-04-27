#!/usr/bin/env python3
"""launchd entry-point for the nightly dream cycle.

The original plist shelled out to `flock -n .brain.lock <python> auto_dream.py`,
which silently no-ops on hosts where the `flock` binary isn't installed
(common on default macOS — `flock(1)` is GNU and not bundled). On those hosts
the dream cycle was running unprotected against sync.sh.

This shim acquires the brain-wide lock via Python `fcntl.flock(LOCK_EX | LOCK_NB)`
on `<brain>/.brain.lock`, then runs auto_dream.run_dream_cycle(). It exits 0
quietly if the lock is held (sync.sh is mid-push), and >0 only on real errors.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--brain-root",
        default=os.environ.get("BRAIN_ROOT", os.path.expanduser("~/.agent")),
        help="Brain root (default: $BRAIN_ROOT or ~/.agent)",
    )
    args = ap.parse_args()

    brain = Path(args.brain_root)
    if not brain.exists():
        sys.stderr.write(f"dream_runner: BRAIN_ROOT not found: {brain}\n")
        return 2

    lock_path = brain / ".brain.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.stderr.write(
                "dream_runner: another brain operation in progress; skipping cycle\n"
            )
            return 0  # not an error — just back off

        # Now safe to import + run. The dream cycle's modules use bare
        # imports (`from promote import ...`, `from text import ...`), so
        # both memory/ and harness/ have to be on sys.path.
        for sub in ("memory", "harness"):
            d = brain / sub
            if d.exists() and str(d) not in sys.path:
                sys.path.insert(0, str(d))
        try:
            import auto_dream  # noqa: WPS433
            auto_dream.run_dream_cycle()
        except Exception as e:
            sys.stderr.write(f"dream_runner: cycle failed: {e!r}\n")
            return 1
        return 0
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


if __name__ == "__main__":
    sys.exit(main())
