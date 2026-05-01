#!/usr/bin/env python3
"""Atomic locked-append helper for the harness.

Bash on macOS doesn't ship `flock`; this gives the harness portable locking
using POSIX fcntl. Reads two arguments + stdin:

    _atomic_append.py <events_path> <payloads_path_or_->

stdin is two JSON lines: the events row (always) and the payload row
(optional, separated by a literal NUL byte). Either gets appended to the
appropriate file under an exclusive lock on `<events_path>.lock`.

Exits 0 on success. Hooks must always exit 0; on internal error this prints
to stderr and still exits 0 so the host (Claude Code) is never blocked by
telemetry.
"""
from __future__ import annotations

import fcntl
import sys
from pathlib import Path


def main() -> int:
    try:
        if len(sys.argv) < 3:
            print("usage: _atomic_append.py <events_path> <payloads_path_or_->", file=sys.stderr)
            return 0
        events_path = Path(sys.argv[1])
        payloads_arg = sys.argv[2]
        payloads_path: Path | None = None if payloads_arg == "-" else Path(payloads_arg)

        events_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = events_path.parent / ".write.lock"
        # Touch the sentinel so flock has something to grab.
        lock_path.touch(exist_ok=True)

        raw = sys.stdin.read()
        # The two payloads are separated by a literal NUL byte.
        parts = raw.split("\x00", 1)
        events_line = parts[0]
        payload_line = parts[1] if len(parts) > 1 else ""

        with lock_path.open("a") as lock_f:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            try:
                if events_line:
                    with events_path.open("a", encoding="utf-8") as ef:
                        ef.write(events_line)
                        if not events_line.endswith("\n"):
                            ef.write("\n")
                if payload_line and payloads_path is not None:
                    with payloads_path.open("a", encoding="utf-8") as pf:
                        pf.write(payload_line)
                        if not payload_line.endswith("\n"):
                            pf.write("\n")
            finally:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        print(f"[_atomic_append] {e!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
