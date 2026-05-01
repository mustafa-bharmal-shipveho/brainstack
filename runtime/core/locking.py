"""Atomic file primitives for the runtime.

Two operations:
  - `locked_append(path, line)` — append one line to a JSONL log under flock.
  - `locked_write(path, content)` — overwrite a file atomically (temp + rename).

Both lock a SENTINEL file (`.{name}.lock` next to the data file), not the
data file itself. Reasoning: brainstack already learned this lesson the hard
way (see `tests/test_concurrent_appends.py`). Locking the data file directly
breaks across `os.replace`, because the lock is on the inode the lock-holder
opened, not the path; after replace, appenders silently write to the orphan
inode.

POSIX-only (depends on `fcntl.flock`). Windows support is future work.
"""
from __future__ import annotations

import fcntl
import os
import tempfile
from pathlib import Path


def sentinel_lock_path(data_path: Path) -> Path:
    """Compute the sentinel-lock-file path next to a data file.

    Example: `/var/log/events.jsonl` -> `/var/log/.events.jsonl.lock`."""
    return data_path.parent / f".{data_path.name}.lock"


def locked_append(path: Path | str, line: str) -> None:
    """Append a line to `path` under an exclusive flock on a sentinel file.

    Parent dirs are created if missing. A trailing newline is added if the
    line doesn't already end with one. Concurrent calls produce one line
    each, in some interleaving — never corrupted bytes.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lock = sentinel_lock_path(p)
    lock.touch(exist_ok=True)
    if not line.endswith("\n"):
        line = line + "\n"
    with lock.open("a") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            with p.open("a", encoding="utf-8") as f:
                f.write(line)
        finally:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)


def locked_write(path: Path | str, content: str) -> None:
    """Atomically overwrite a file: write to a sibling temp, then rename.

    Concurrent callers race on the rename, but each rename is itself atomic,
    so the on-disk file is always either the previous version or one
    complete new version — never a half-written file. The flock serializes
    the temp-write phase as well.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lock = sentinel_lock_path(p)
    lock.touch(exist_ok=True)
    with lock.open("a") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            # NamedTemporaryFile in same dir so rename is on the same FS.
            fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=p.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                os.replace(tmp, p)
            except Exception:
                # If anything failed before replace, clean up the temp.
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass
                raise
        finally:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)


__all__ = ["locked_append", "locked_write", "sentinel_lock_path"]
