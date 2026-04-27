"""Atomic write helpers for memory state.

A SIGKILL or OOM hitting an in-place rewrite (`open(path, "w")` → write →
close) leaves the file truncated. Subsequent reads see an empty (torn) file
until something repopulates it. The dream cycle does this on the episodic
JSONL; promote/review_state do this on candidate JSON files.

`atomic_write_text` and `atomic_write_bytes` write to a sibling temp file,
fsync, then rename over the target. The rename is atomic on POSIX (and
ReplaceFile on Windows under recent Python). A SIGKILL during the temp-write
phase leaves the original file untouched and a stray `.tmp` next to it; the
next sync run cleans the temp up.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def atomic_write_bytes(path: os.PathLike[str] | str, data: bytes) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        # Use low-level open so we can fsync before close.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, p)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def atomic_write_text(path: os.PathLike[str] | str, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: os.PathLike[str] | str, obj: Any, *, indent: int = 2) -> None:
    atomic_write_text(path, json.dumps(obj, indent=indent))


def cleanup_stale_tmp(directory: os.PathLike[str] | str) -> int:
    """Best-effort cleanup of `.tmp` siblings left by killed writes.

    Returns the count removed. Safe to call from sync.sh on every run.
    """
    d = Path(directory)
    if not d.exists():
        return 0
    removed = 0
    for tmp in d.rglob("*.tmp"):
        try:
            tmp.unlink()
            removed += 1
        except OSError:
            continue
    return removed
