#!/usr/bin/env bash
# Sync the brain at $BRAIN_ROOT (default ~/.agent/) to its private remote.
#
# Behavior:
#   - flock on $BRAIN_ROOT/.brain.lock (Python fcntl helper if `flock` binary
#     is missing) so we never run concurrent with the dream cycle.
#   - exit 0 if there are no changes to commit.
#   - run JSONL secret-scrubber over episodic logs (rewrites in place).
#   - run trufflehog (REQUIRED — fails closed if missing).
#   - run the redact pre-commit filter (already wired as a git hook).
#   - commit + push.
#
# Intended to be invoked by launchd hourly. See docs/git-sync.md.
#
# Exit codes:
#   0 = no-op or successful push
#   1 = error (lock contention, scrubber failure, trufflehog hit, push fail)
#   2 = misconfiguration (missing tools, missing brain)
set -euo pipefail

BRAIN_ROOT="${BRAIN_ROOT:-$HOME/.agent}"
LOCK_FILE="$BRAIN_ROOT/.brain.lock"
LOG_FILE="$BRAIN_ROOT/sync.log"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# ---- Resolve a Python that's >= 3.10 ----
if ! "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    for cand in python3.13 python3.12 python3.11 python3.10; do
        if command -v "$cand" >/dev/null; then
            PYTHON_BIN="$cand"
            break
        fi
    done
fi

if ! "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    echo "$(date -u +%FT%TZ) sync: Python >= 3.10 required, none found" >&2
    exit 2
fi

if [ ! -d "$BRAIN_ROOT" ]; then
    echo "$(date -u +%FT%TZ) sync: BRAIN_ROOT not found: $BRAIN_ROOT" >&2
    exit 0
fi

if [ ! -d "$BRAIN_ROOT/.git" ]; then
    echo "$(date -u +%FT%TZ) sync: $BRAIN_ROOT is not a git repo; nothing to sync" >&2
    exit 0
fi

# ---- Acquire exclusive lock (Python fallback if flock missing) ----
acquire_lock() {
    if command -v flock >/dev/null 2>&1; then
        exec 9> "$LOCK_FILE"
        flock -n 9
        return $?
    fi
    # Python fallback — uses fcntl.flock (LOCK_EX|LOCK_NB)
    "$PYTHON_BIN" - "$LOCK_FILE" <<'PY' &
import fcntl, os, signal, sys, time
lock_path = sys.argv[1]
fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(11)  # mimic flock(1) "would block" exit
# Hold the lock until parent signals us
def _release(*_):
    fcntl.flock(fd, fcntl.LOCK_UN)
    sys.exit(0)
signal.signal(signal.SIGTERM, _release)
signal.signal(signal.SIGINT, _release)
# Block waiting for signal
signal.pause()
PY
    LOCK_PID=$!
    # Give the helper a moment to fail if it's going to
    sleep 0.2
    if ! kill -0 "$LOCK_PID" 2>/dev/null; then
        return 11
    fi
    trap 'kill -TERM '"$LOCK_PID"' 2>/dev/null || true' EXIT
    return 0
}

if ! acquire_lock; then
    echo "$(date -u +%FT%TZ) sync: another brain operation in progress, skipping" >> "$LOG_FILE"
    exit 0
fi

cd "$BRAIN_ROOT"

# Reusable refresh helper — called both before AND after the sync. The
# pre-sync call refreshes the candidate count for surfaces; the post-sync
# call updates the sync-status field after we've written the final log
# line (so a successful push clears a stale "blocked"/"stale" warning that
# was true at the start of the run — Codex 2026-05-04 P2).
_refresh_pending_summary() {
    if [ -f "$BRAIN_ROOT/tools/render_pending_summary.py" ]; then
        PYTHON_BIN_FOR_RENDER="${PYTHON_BIN:-python3}"
        if command -v "$PYTHON_BIN_FOR_RENDER" >/dev/null 2>&1; then
            "$PYTHON_BIN_FOR_RENDER" "$BRAIN_ROOT/tools/render_pending_summary.py" \
                --brain "$BRAIN_ROOT" 2>/dev/null || true
        fi
    fi
}

# Pre-sync refresh: candidate counts (drift, sync stat) before scrubbing
_refresh_pending_summary

# ---- Sync-time JSONL scrubber (overwrites secrets in episodic JSONL) ----
JSONL_SCRUBBER="$BRAIN_ROOT/tools/redact_jsonl.py"
if [ -x "$JSONL_SCRUBBER" ] || [ -f "$JSONL_SCRUBBER" ]; then
    # Build a list of scrub targets that actually exist. data-layer/ is
    # only created on first dashboard export, so a fresh brain doesn't
    # have it; passing a missing path makes the scrubber exit 2 (fatal).
    SCRUB_TARGETS=()
    [ -d "$BRAIN_ROOT/memory/episodic" ] && SCRUB_TARGETS+=("$BRAIN_ROOT/memory/episodic")
    [ -d "$BRAIN_ROOT/data-layer" ] && SCRUB_TARGETS+=("$BRAIN_ROOT/data-layer")

    if [ "${#SCRUB_TARGETS[@]}" -gt 0 ]; then
        # Scrubber returns 1 if it changed files; we still want to proceed.
        # rc=2 is the only fatal case.
        set +e
        "$PYTHON_BIN" "$JSONL_SCRUBBER" "${SCRUB_TARGETS[@]}" 2>>"$LOG_FILE"
        rc=$?
        set -e
        if [ "$rc" -eq 2 ]; then
            echo "$(date -u +%FT%TZ) sync: JSONL scrubber failed (rc=2); refusing to push" >> "$LOG_FILE"
            exit 1
        fi
        if [ "$rc" -eq 1 ]; then
            echo "$(date -u +%FT%TZ) sync: JSONL scrubber rewrote secrets in episodic logs" >> "$LOG_FILE"
        fi
    fi

    # Best-effort: clean up stale .tmp siblings left by killed atomic writes.
    # Pass BRAIN_ROOT as argv[1] (NOT shell-interpolated into the source) so
    # an exotic BRAIN_ROOT value (containing apostrophes / quote-escapes)
    # cannot become attacker-controlled Python.
    "$PYTHON_BIN" -c '
import os, sys
brain = sys.argv[1]
sys.path.insert(0, os.path.join(brain, "memory"))
from _atomic import cleanup_stale_tmp
n = cleanup_stale_tmp(os.path.join(brain, "memory"))
if n:
    print(f"sync: cleaned {n} stale .tmp file(s)")
' "$BRAIN_ROOT" 2>>"$LOG_FILE" || true
else
    echo "$(date -u +%FT%TZ) sync: WARNING redact_jsonl.py missing at $JSONL_SCRUBBER" >> "$LOG_FILE"
fi

# ---- Required: a serverless secret scanner (trufflehog or gitleaks) ----
SCANNER=""
if command -v trufflehog >/dev/null 2>&1; then
    SCANNER="trufflehog"
elif command -v gitleaks >/dev/null 2>&1; then
    SCANNER="gitleaks"
fi

if [ -z "$SCANNER" ]; then
    if [ "${SYNC_ALLOW_NO_SCANNER:-}" = "1" ]; then
        echo "$(date -u +%FT%TZ) sync: WARNING no scanner installed but SYNC_ALLOW_NO_SCANNER=1; continuing" >> "$LOG_FILE"
    else
        echo "$(date -u +%FT%TZ) sync: ERROR no secret scanner installed (trufflehog or gitleaks)" >&2
        echo "    Install one:" >&2
        echo "      brew install trufflehog" >&2
        echo "      brew install gitleaks" >&2
        echo "    Or set SYNC_ALLOW_NO_SCANNER=1 to skip (NOT RECOMMENDED)." >&2
        exit 2
    fi
fi

# Optional: $BRAIN_ROOT/.trufflehog-exclude.txt lets the user exclude paths
# from the local scan (e.g. .git/objects/ which carries historical commits'
# objects). Going-forward content is already covered by the pre-commit hook
# and JSONL scrubber; the server-side workflow catches --no-verify bypasses.
TH_EXCLUDE="$BRAIN_ROOT/.trufflehog-exclude.txt"

case "$SCANNER" in
    trufflehog)
        TH_ARGS=(filesystem . --no-update --fail)
        [ -f "$TH_EXCLUDE" ] && TH_ARGS+=(--exclude-paths "$TH_EXCLUDE")
        if ! trufflehog "${TH_ARGS[@]}" >/dev/null 2>>"$LOG_FILE"; then
            echo "$(date -u +%FT%TZ) sync: trufflehog flagged secrets; refusing to push" >> "$LOG_FILE"
            exit 1
        fi
        ;;
    gitleaks)
        if ! gitleaks detect --source . --no-git --redact >/dev/null 2>>"$LOG_FILE"; then
            echo "$(date -u +%FT%TZ) sync: gitleaks flagged secrets; refusing to push" >> "$LOG_FILE"
            exit 1
        fi
        ;;
esac

# ---- Stage all changes ----
git add -A

# Anything to commit?
if git diff --cached --quiet; then
    echo "$(date -u +%FT%TZ) sync: no changes" >> "$LOG_FILE"
    exit 0
fi

# ---- Commit + push (pre-commit hook runs redact.py) ----
TS="$(date -u +%FT%TZ)"
if git commit -q -m "auto: $TS" 2>>"$LOG_FILE"; then
    if git push -q 2>>"$LOG_FILE"; then
        echo "$TS sync: pushed" >> "$LOG_FILE"
    else
        echo "$TS sync: commit succeeded but push failed; brain is committed locally" >> "$LOG_FILE"
        # Refresh after writing the final log line so PENDING_REVIEW.md
        # reflects the new "blocked" / "stale" state (Codex 2026-05-04 P2).
        _refresh_pending_summary
        exit 1
    fi
else
    echo "$TS sync: commit blocked (likely by redact pre-commit hook)" >> "$LOG_FILE"
    _refresh_pending_summary
    exit 1
fi

# Post-sync refresh on success — clears any stale "blocked"/"stale"
# warning from the previous run by re-reading the now-updated sync.log.
_refresh_pending_summary
