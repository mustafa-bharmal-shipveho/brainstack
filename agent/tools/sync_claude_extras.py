#!/usr/bin/env python3
"""LaunchAgent entrypoint: run claude_session_adapter + claude_misc_adapter
under the same fcntl lock that brainstack's auto-migrate-all uses.

Why a Python wrapper instead of bash + flock(1): macOS doesn't ship
`flock(1)`, and brainstack's own dispatcher uses Python `fcntl.flock` on
`<brain>/.auto-migrate.lock`. Using the same primitive guarantees we
won't race the dispatcher's hourly cursor/codex pass.

Lifecycle:
    1. Open <brain>/.auto-migrate.lock (LOCK_EX, 90s timeout)
    2. Run claude_session_adapter.py (incremental — only new sessions)
    3. Run claude_misc_adapter.py (incremental — mtime-based)
    4. Release lock

Per-adapter failures are logged but don't abort the run. All output goes
to <brain>/claude-extras.log (append-only).

Invoked by: ~/Library/LaunchAgents/com.brainstack.claude-extras.plist
"""
from __future__ import annotations

import datetime
import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

BRAIN_ROOT = Path(os.environ.get("BRAIN_ROOT", str(Path.home() / ".agent")))
LOCK_PATH = BRAIN_ROOT / ".auto-migrate.lock"
LOG_PATH = BRAIN_ROOT / "claude-extras.log"
LOCK_TIMEOUT = 90.0  # seconds

# Locate the python interpreter and tools dir. Default to brainstack's venv
# but honor explicit overrides.
PYTHON = os.environ.get(
    "PYTHON",
    "/Users/mustafa.bharmal/Documents/brainstack/.venv/bin/python",
)
TOOLS_DIR = BRAIN_ROOT / "tools"


def _log(msg: str) -> None:
    """Append a timestamped line to the log file (and stdout for LaunchAgent
    StandardOutPath capture)."""
    line = f"{datetime.datetime.now(datetime.timezone.utc).isoformat()} {msg}\n"
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as f:
            f.write(line)
    except OSError:
        pass
    sys.stdout.write(line)
    sys.stdout.flush()


def _acquire_lock(lock_path: Path, timeout: float):
    """Acquire an exclusive flock on `lock_path`. Returns the open fd or
    raises TimeoutError. Same pattern as auto_migrate_all in
    migrate_dispatcher.py."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "w")
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                fd.close()
                raise TimeoutError(f"could not acquire {lock_path} within {timeout}s")
            time.sleep(0.1)


def _release_lock(fd) -> None:
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    fd.close()


def _run_adapter(label: str, script: Path, extra_args: list[str]) -> int:
    """Run one adapter script; capture output to the log. Return exit code.

    `extra_args` carries the per-adapter destination flags so the brain
    root is propagated explicitly. Without this the adapters defaulted
    to ~/.agent regardless of $BRAIN_ROOT, breaking custom installs
    (Codex 2026-05-04 P2)."""
    if not script.is_file():
        _log(f"[{label}] FATAL: script not found: {script}")
        return 1
    _log(f"[{label}] starting")
    try:
        proc = subprocess.run(
            [PYTHON, str(script), *extra_args],
            capture_output=True,
            text=True,
            timeout=600,  # 10 min ceiling per adapter
        )
    except subprocess.TimeoutExpired:
        _log(f"[{label}] TIMEOUT after 600s — killed")
        return 1
    except OSError as e:
        _log(f"[{label}] FATAL: {e}")
        return 1
    if proc.stdout:
        _log(f"[{label}] stdout:\n{proc.stdout.rstrip()}")
    if proc.stderr:
        _log(f"[{label}] stderr:\n{proc.stderr.rstrip()}")
    _log(f"[{label}] done (exit {proc.returncode})")
    return proc.returncode


def main() -> int:
    _log("=== sync_claude_extras run starting ===")
    _log(f"  BRAIN_ROOT={BRAIN_ROOT}")
    _log(f"  PYTHON={PYTHON}")

    if not Path(PYTHON).is_file():
        _log(f"FATAL: python interpreter not found: {PYTHON}")
        return 1

    try:
        lock_fd = _acquire_lock(LOCK_PATH, LOCK_TIMEOUT)
    except TimeoutError as e:
        _log(f"WARN: {e} — skipping run")
        return 0  # not an error — another sync is running, fine to skip

    _log(f"  lock acquired: {LOCK_PATH}")
    try:
        rc1 = _run_adapter(
            "claude_session_adapter",
            TOOLS_DIR / "claude_session_adapter.py",
            ["--dst", str(BRAIN_ROOT)],
        )
        rc2 = _run_adapter(
            "claude_misc_adapter",
            TOOLS_DIR / "claude_misc_adapter.py",
            ["--brain", str(BRAIN_ROOT)],
        )
        _log(f"=== sync_claude_extras done (session={rc1}, misc={rc2}) ===\n")
        return 0 if rc1 == 0 and rc2 == 0 else 1
    finally:
        _release_lock(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
