"""Cross-platform locked append for episodic JSONL writes.

POSIX `write(2)` in O_APPEND mode is atomic for payloads up to PIPE_BUF
(4 KB on Linux, 512 B minimum per POSIX). Most episodic entries fit,
but failure entries with reflection + context + detail can exceed that,
and two harness hooks writing from the same process (or from two
sessions on the same repo) can interleave bytes mid-line. Silent
corruption is worse than a visible error because every downstream
reader (`auto_dream.py`, `cluster.py`, `context_budget.py`,
`show.py`) skips `JSONDecodeError` lines without surfacing the loss.

This module serializes appends with `fcntl.flock(LOCK_EX)` on a
SENTINEL sibling file (`<jsonl>.lock`), NOT the data file itself.
Why: the dream cycle's atomic rewrite uses `os.replace`, which swaps
the data file's inode out from under any process holding flock on the
old inode. Locking the sentinel decouples the lock identity from the
data file's lifetime, so atomic replace is safe for both writers.

On platforms without `fcntl` (native Windows Python) the lock is a
no-op and behavior matches the pre-lock baseline. WSL, git-bash via
Cygwin, macOS, and Linux all provide `fcntl`.
"""
import datetime
import json
import os

try:
    import fcntl  # POSIX
    _HAVE_FLOCK = True
except ImportError:
    _HAVE_FLOCK = False

# S5 rotation threshold (20 MiB) — matches agent/memory/_atomic.ROTATE_BYTES
# and runtime/core/events.EVENT_LOG_ROTATE_BYTES. Read at CALL time by
# `append_jsonl` (the `max_bytes` kwarg defaults to None and resolves to
# this constant inside the body), so callers that thread no threshold
# through — `sdk.append_episodic`, the harness hooks — still pick up a
# monkeypatched value.
ROTATE_BYTES = 20 * 1024 * 1024


def _sentinel_path(data_path: str) -> str:
    """Return the lock sentinel sibling for a data file path."""
    return data_path + ".lock"


def _today() -> str:
    """UTC calendar day as `YYYY-MM-DD`, the stamp `rolled_name` inserts."""
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def rolled_name(path: str, day: str) -> str:
    """Compute the rotated sibling name for `path` on day `day`.

    Only the FINAL suffix counts as the extension, so a compound stem
    survives: `AGENT_LEARNINGS.jsonl` -> `AGENT_LEARNINGS.<day>.jsonl`. On
    a same-day collision a counter goes before the suffix
    (`AGENT_LEARNINGS.<day>.1.jsonl`, `.2`, ...), so a busy day that rolls
    several times never overwrites an earlier roll.
    """
    directory, name = os.path.split(path)
    stem, suffix = os.path.splitext(name)
    candidate = os.path.join(directory, f"{stem}.{day}{suffix}")
    counter = 0
    while os.path.exists(candidate):
        counter += 1
        candidate = os.path.join(directory, f"{stem}.{day}.{counter}{suffix}")
    return candidate


def _rotate_if_oversize(path: str, max_bytes: int) -> str | None:
    """Rename `path` out of the way when it is STRICTLY LARGER than
    `max_bytes`, returning the rolled path (or `None` if untouched).

    Strictly greater, not at-or-over: a file sitting exactly at the
    threshold must not roll, or a brain hovering at the limit would roll on
    every single append. Callers must already hold the sentinel lock.
    """
    if not max_bytes:
        return None
    try:
        if os.path.getsize(path) <= max_bytes:
            return None
    except OSError:
        return None  # missing / unreadable — nothing to roll
    rolled = rolled_name(path, _today())
    try:
        os.rename(path, rolled)
    except OSError:
        return None  # keep appending to the current file
    return rolled


def append_jsonl(path: str, entry: dict, *, max_bytes: int | None = None) -> dict:
    """Serialize `entry` to one JSON line and append to `path`.

    Lock identity lives on `path + ".lock"` so a concurrent atomic
    rewrite of `path` (which swaps its inode) does not invalidate
    in-flight appenders' lock acquisitions.

    `max_bytes` caps the current file's size (falling back to
    `ROTATE_BYTES` when omitted). A file strictly larger than the cap is
    renamed to a dated sibling before this entry is appended, so the
    append always lands in a file under the cap. The rename happens while
    we hold the sentinel lock — the same lock every other appender and the
    dream cycle's rewrite take — so a roll can never land between another
    writer's open() and write(). Rolled files are never appended to again.

    Failure handling: this hook fires per tool call, so any unhandled
    exception will dump a traceback to the user's terminal. Catch all
    OSErrors (read-only file, full disk, missing dir we can't create,
    permission denied) and degrade silently — the dream cycle's flag-
    on-no-progress is a better signal than crashing each tool call.
    """
    payload = (json.dumps(entry) + "\n").encode("utf-8")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        return entry  # Can't even create the dir — give up silently
    # Resolved here, not as a def-time default, so a monkeypatched
    # ROTATE_BYTES reaches callers that pass no threshold.
    limit = ROTATE_BYTES if max_bytes is None else max_bytes
    sentinel = _sentinel_path(path)

    if _HAVE_FLOCK:
        # Open the sentinel separately, flock it, then open + write the data
        # file. Close the data file before releasing the sentinel — that way,
        # if another process is mid-rewrite (holding sentinel), our open + write
        # won't race their os.replace.
        try:
            lock_fd = os.open(sentinel, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError:
            return entry
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                _rotate_if_oversize(path, limit)
                with open(path, "ab") as f:
                    f.write(payload)
                    f.flush()
            except OSError:
                # File is read-only / disk full / etc. Don't propagate —
                # this would crash every tool call and the user can't do
                # anything about it from inside Claude Code anyway.
                pass
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
    else:
        # Windows fallback — no locking; matches pre-lock baseline.
        try:
            _rotate_if_oversize(path, limit)
            with open(path, "ab") as f:
                f.write(payload)
                f.flush()
        except OSError:
            pass
    return entry
