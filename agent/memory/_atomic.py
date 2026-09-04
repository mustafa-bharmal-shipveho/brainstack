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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# S5 rotation threshold (20 MiB) — shared naming rule with
# agent/harness/hooks/_episodic_io.ROTATE_BYTES and
# runtime/core/events.EVENT_LOG_ROTATE_BYTES.
ROTATE_BYTES = 20 * 1024 * 1024


def _today() -> str:
    """UTC calendar day as `YYYY-MM-DD`, the stamp `rolled_name` inserts."""
    return datetime.now(timezone.utc).date().isoformat()


def rolled_name(path: Path, day: str) -> Path:
    """Compute the rotated sibling name for `path` on day `day`.

    Only the FINAL suffix counts as the extension, so a compound stem
    survives: `AGENT_LEARNINGS.jsonl` -> `AGENT_LEARNINGS.<day>.jsonl`. On
    a same-day collision a counter goes before the suffix
    (`AGENT_LEARNINGS.<day>.1.jsonl`, `.2`, ...), so a day that rolls
    several times never overwrites an earlier roll.
    """
    path = Path(path)
    stem, suffix = path.stem, path.suffix
    candidate = path.parent / f"{stem}.{day}{suffix}"
    counter = 0
    while candidate.exists():
        counter += 1
        candidate = path.parent / f"{stem}.{day}.{counter}{suffix}"
    return candidate


def rotate_if_oversize(
    path: Path, *, max_bytes: int | None = None, today: str | None = None
) -> Path | None:
    """Rename `path` out of the way when it is STRICTLY LARGER than
    `max_bytes`, returning the rolled path (or `None` if untouched).

    Strictly greater, not at-or-over: a file sitting exactly at the
    threshold must not roll, or a brain hovering at the limit would roll on
    every write. `max_bytes` defaults to `ROTATE_BYTES` resolved HERE
    rather than as a def-time default, so callers that thread no threshold
    through (the codex / claude-session adapters) still see a
    monkeypatched value.

    This is the rotation site for the full-file REWRITERS. They read the
    whole namespace file and write it back, so without a roll first an
    oversize file is re-read and re-written on every import and never
    shrinks. Callers hold the `.auto-migrate.lock` for their namespace.
    """
    limit = ROTATE_BYTES if max_bytes is None else max_bytes
    path = Path(path)
    if not limit:
        return None
    try:
        if path.stat().st_size <= limit:
            return None
    except OSError:
        return None  # missing / unreadable — nothing to roll
    rolled = rolled_name(path, today or _today())
    try:
        os.replace(path, rolled)
    except OSError:
        return None
    return rolled


def episodic_files(current: Path) -> list[Path]:
    """Rolled siblings of `current` (ascending by name), then `current`.

    The read side of rotation: history that moved into a rolled file must
    stay visible to `sdk.stats`, the dream cycle, the consolidator and the
    adapters' dedup preload. The glob is `<stem>*<suffix>`, which matches
    every roll of this stream and skips sibling streams like
    `_imported.jsonl`. Order is name-ascending, NOT chronological — byte
    order puts `AGENT_LEARNINGS.<day>.1.jsonl` before
    `AGENT_LEARNINGS.<day>.jsonl`.

    `current` is always last, even when it does not exist yet (every
    consumer already tolerates a missing episodic file).
    """
    current = Path(current)
    pattern = f"{current.stem}*{current.suffix}"
    try:
        rolled = sorted(
            p for p in current.parent.glob(pattern) if p.name != current.name
        )
    except OSError:
        rolled = []
    return [*rolled, current]


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
