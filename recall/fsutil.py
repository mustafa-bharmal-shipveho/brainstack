"""Filesystem helpers shared across `recall`.

Small, stdlib-only, and deliberately free of `recall.*` imports so the
lowest-level writers (lint, health, utilization) can all use them without
dragging retrieval into their import graph.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` atomically and symlink-safely.

    `mkstemp` creates a fresh `O_EXCL` file in the SAME directory as the
    target: same filesystem, so `os.replace` is a true atomic rename, and
    it can never follow a temp symlink someone planted. A crash mid-write
    therefore leaves the PREVIOUS file intact rather than a truncated one.

    `newline=""` disables newline translation, so the bytes on disk are
    exactly the text handed in — this matters for the lint writers, which
    round-trip user files.

    Raises on failure, having removed the temp file first. Callers that
    must not raise (a report we cannot write is not a crash) catch
    `OSError` themselves.
    """
    path = Path(path)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


__all__ = ["atomic_write_text"]
