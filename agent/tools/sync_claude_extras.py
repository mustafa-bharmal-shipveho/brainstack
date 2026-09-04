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
    4. Run digest_cli.py incremental, if the user opted in (own timeout,
       own per-run budget — see below)
    5. Release lock

Per-adapter failures are logged but don't abort the run. All output goes
to <brain>/claude-extras.log (append-only).

Digest budget: a single session digest costs several minutes of LLM
time (claude -p / haiku), so on a busy hour the digest step used to run
past the shared 600s adapter timeout and get killed — `digest_cli.py
incremental` exiting non-zero every hour even though the session + misc
mirrors succeeded, and the backlog was invisible. The digest step now
gets its own (longer) timeout, `BRAINSTACK_DIGEST_TIMEOUT_S` (default
1800s), separate from the 600s used for the session/misc adapters, and
`digest_cli.py incremental` itself is bounded by `--limit` /
`--max-seconds` (from `BRAINSTACK_DIGEST_LIMIT` /
`BRAINSTACK_DIGEST_MAX_SECONDS`, defaults 3 / 1500) so it stops cleanly
before the process-level timeout ever fires. Its
`digests: processed=... pending=... elapsed_s=... budget_hit=...`
summary line is quoted into this log; the CLI itself writes
`runtime/digest_status.json` from the same numbers, so a backlog is
visible instead of silently growing.

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
ADAPTER_TIMEOUT = 600.0  # seconds — session/misc adapters (unchanged)

# Locate the python interpreter and tools dir. Honor explicit overrides.
PYTHON = os.environ.get("PYTHON", sys.executable)
TOOLS_DIR = BRAIN_ROOT / "tools"


def _env_num(name: str, default, cast):
    """Read `$name` as `cast`, falling back to `default` when it is
    unset, blank, or unparseable. An operator's typo must degrade to the
    shipped budget, never to a crash in the hourly job."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return cast(raw)
    except ValueError:
        return default


# Digest step's own budget — separate from the 600s adapter timeout so a
# busy hour (many new sessions) can't get killed mid-summarize.
DIGEST_TIMEOUT_S = _env_num("BRAINSTACK_DIGEST_TIMEOUT_S", 1800.0, float)
DIGEST_LIMIT = _env_num("BRAINSTACK_DIGEST_LIMIT", 3, int)
DIGEST_MAX_SECONDS = _env_num("BRAINSTACK_DIGEST_MAX_SECONDS", 1500.0, float)

_DIGEST_SUMMARY_PREFIX = "digests: "


def _digest_summary_line(stdout: str) -> str:
    """The LAST `digests: ...` line the digest step printed, or `""` if
    it printed none (e.g. it errored before getting that far).

    Quoted into the log verbatim, never parsed: the CLI owns
    `runtime/digest_status.json` and writes it from the numbers it
    already holds."""
    for line in reversed((stdout or "").splitlines()):
        if line.strip().startswith(_DIGEST_SUMMARY_PREFIX):
            return line.strip()
    return ""


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


def _run_adapter(label: str, script: Path, extra_args: list[str],
                  *, timeout: float = ADAPTER_TIMEOUT) -> tuple[int, str]:
    """Run one adapter script; capture output to the log. Return
    `(exit_code, stdout)` — callers that don't need stdout just ignore
    the second element.

    `extra_args` carries the per-adapter destination flags so the brain
    root is propagated explicitly. Without this the adapters defaulted
    to ~/.agent regardless of $BRAIN_ROOT, breaking custom installs
    (Codex 2026-05-04 P2).

    `timeout` defaults to the shared 600s adapter ceiling, but the
    digest step passes its own (longer) `DIGEST_TIMEOUT_S` — a single
    session digest costs minutes of LLM time, so it needs more room
    than the near-instant session/misc mirrors."""
    if not script.is_file():
        _log(f"[{label}] FATAL: script not found: {script}")
        return 1, ""
    _log(f"[{label}] starting")
    try:
        proc = subprocess.run(
            [PYTHON, str(script), *extra_args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        _log(f"[{label}] TIMEOUT after {timeout:.0f}s — killed")
        return 1, ""
    except OSError as e:
        _log(f"[{label}] FATAL: {e}")
        return 1, ""
    if proc.stdout:
        _log(f"[{label}] stdout:\n{proc.stdout.rstrip()}")
    if proc.stderr:
        _log(f"[{label}] stderr:\n{proc.stderr.rstrip()}")
    _log(f"[{label}] done (exit {proc.returncode})")
    return proc.returncode, (proc.stdout or "")


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
        rc1, _out1 = _run_adapter(
            "claude_session_adapter",
            TOOLS_DIR / "claude_session_adapter.py",
            ["--dst", str(BRAIN_ROOT)],
        )
        rc2, _out2 = _run_adapter(
            "claude_misc_adapter",
            TOOLS_DIR / "claude_misc_adapter.py",
            ["--brain", str(BRAIN_ROOT)],
        )
        # Digest layer: only run when the user has opted in via
        # `./install.sh --setup-digests` (which writes the marker file).
        # Unconfigured installs skip silently — no surprise LLM calls.
        rc3 = 0
        if (BRAIN_ROOT / ".digests-enabled").is_file():
            _log(f"  digest budget: limit={DIGEST_LIMIT} "
                 f"max_seconds={DIGEST_MAX_SECONDS} "
                 f"timeout={DIGEST_TIMEOUT_S}")
            rc3, out3 = _run_adapter(
                "digest_cli_incremental",
                TOOLS_DIR / "digest_cli.py",
                ["incremental",
                 "--limit", str(DIGEST_LIMIT),
                 "--max-seconds", str(DIGEST_MAX_SECONDS)],
                timeout=DIGEST_TIMEOUT_S,
            )
            summary_line = _digest_summary_line(out3)
            if summary_line:
                _log(f"[digest_cli_incremental] {summary_line}")
            elif rc3 == 0:
                # Completed but printed no summary line — shouldn't
                # happen, but don't let a missing receipt masquerade
                # as a clean run.
                _log("[digest_cli_incremental] WARN: no digest summary "
                     "line found in stdout")
        else:
            _log("  digest layer not enabled (no .digests-enabled marker); "
                 "run `./install.sh --setup-digests` to opt in")
        _log(
            f"=== sync_claude_extras done "
            f"(session={rc1}, misc={rc2}, digests={rc3}) ===\n"
        )
        return 0 if rc1 == 0 and rc2 == 0 and rc3 == 0 else 1
    finally:
        _release_lock(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
