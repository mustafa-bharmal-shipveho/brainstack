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
from datetime import datetime, timezone
from pathlib import Path


def sentinel_lock_path(data_path: Path) -> Path:
    """Compute the sentinel-lock-file path next to a data file.

    Example: `/var/log/events.jsonl` -> `/var/log/.events.jsonl.lock`."""
    return data_path.parent / f".{data_path.name}.lock"


def _today() -> str:
    """UTC calendar day as `YYYY-MM-DD` — the stamp `rolled_name` inserts."""
    return datetime.now(timezone.utc).date().isoformat()


def rolled_name(path: Path, day: str) -> Path:
    """Compute the rotated sibling name for `path` on day `day`.

    Only the FINAL suffix is treated as the extension, so a compound stem
    survives: `events.log.jsonl` -> `events.log.<day>.jsonl`. On a same-day
    collision a counter is inserted before the suffix
    (`events.log.<day>.1.jsonl`, `.2`, ...), so a log that rolls several
    times in one day never overwrites an earlier roll.
    """
    path = Path(path)
    stem, suffix = path.stem, path.suffix
    candidate = path.parent / f"{stem}.{day}{suffix}"
    counter = 0
    while candidate.exists():
        counter += 1
        candidate = path.parent / f"{stem}.{day}.{counter}{suffix}"
    return candidate


def iter_log_paths(path: Path) -> list[Path]:
    """Rolled siblings of `path` (ascending by name), then `path` itself.

    The glob is `<stem>*<suffix>`, which matches every roll of this log and
    nothing else: sentinel locks are dotfiles (`.events.log.jsonl.lock`),
    temp files end in `.tmp`, and an unrelated `other.log.jsonl` has a
    different stem. Order is name-ascending, NOT chronological — byte order
    puts `events.log.2026-09-04.1.jsonl` (the SECOND roll of that day)
    before `events.log.2026-09-04.jsonl` (the first). Callers that need a
    timeline must re-sort by each record's own timestamp; this list is only
    "every file of the stream, current one last".
    """
    path = Path(path)
    pattern = f"{path.stem}*{path.suffix}"
    try:
        rolled = sorted(p for p in path.parent.glob(pattern) if p.name != path.name)
    except OSError:
        rolled = []
    return [*rolled, path]


def locked_append(path: Path | str, line: str, *, rotate_bytes: int | None = None) -> None:
    """Append a line to `path` under an exclusive flock on a sentinel file.

    Parent dirs are created if missing. A trailing newline is added if the
    line doesn't already end with one. Concurrent calls produce one line
    each, in some interleaving — never corrupted bytes.

    `rotate_bytes` caps the current file's size: when set, a file STRICTLY
    LARGER than the threshold is renamed to `rolled_name(path, <today>)`
    before this line is appended, so the append lands in a fresh file. A
    file sitting exactly at the threshold is not oversize. Leave
    `rotate_bytes` unset (the default) for logs that must not rotate.

    The rename happens while we hold the sentinel lock, which is also what
    every other appender and the dream cycle's rewrite take — so a roll can
    never land between another writer's open() and write().
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
            if rotate_bytes:
                try:
                    oversize = p.stat().st_size > rotate_bytes
                except OSError:
                    oversize = False
                if oversize:
                    try:
                        os.replace(p, rolled_name(p, _today()))
                    except OSError:
                        pass  # keep appending to the current file
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


__all__ = [
    "iter_log_paths", "locked_append", "locked_write", "rolled_name",
    "sentinel_lock_path",
]
