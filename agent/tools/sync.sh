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
        # rc=2 is the only fatal case. --brain-root points the scrubber at
        # THIS brain's redact-private.txt (its default is ~/.agent, which
        # silently drops private patterns on non-default BRAIN_ROOT installs).
        set +e
        "$PYTHON_BIN" "$JSONL_SCRUBBER" --brain-root "$BRAIN_ROOT" "${SCRUB_TARGETS[@]}" 2>>"$LOG_FILE"
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
        # Log marker BEFORE exiting so render_pending_summary's
        # _check_sync_status can classify the sync as blocked (the literal
        # phrase "no secret scanner installed" is the detection contract).
        echo "$(date -u +%FT%TZ) sync: no secret scanner installed; skipping push" >> "$LOG_FILE"
        echo "$(date -u +%FT%TZ) sync: ERROR no secret scanner installed (trufflehog or gitleaks)" >&2
        echo "    Install one:" >&2
        echo "      brew install trufflehog" >&2
        echo "      brew install gitleaks" >&2
        echo "    Or: ./install.sh --install-scanner" >&2
        echo "    Or set SYNC_ALLOW_NO_SCANNER=1 to skip (NOT RECOMMENDED)." >&2
        echo "    recall doctor will show this failure." >&2
        exit 2
    fi
fi

# Path- and value-level scan tuning both live next to the brain and are
# read by scan_gate.py, not by this script:
#   .trufflehog-exclude.txt      paths the scanner never looks at
#   .secret-scan-allowlist.txt   regexes for known false-positive values
# Going-forward content is already covered by the pre-commit hook and the
# JSONL scrubber; the server-side workflow catches --no-verify bypasses.

# ---- Scan, then quarantine per-file (NOT fail-closed globally) ----
#
# History: this gate used to be `trufflehog --fail` + `exit 1`. One
# unverified false positive anywhere in the brain — a bare git SHA in a
# logged `gh api ...?ref=<sha>` URL, which the `Circle` detector reads as
# a CircleCI token — blocked every hourly sync for a MONTH, silently.
# A whole month of memories sat unpushed and nothing surfaced it.
#
# Policy now: if we're not sure about a file, skip THAT FILE and push
# everything else. scan_gate.py prints the repo-relative path of each
# risky file on stdout; we unstage exactly those and commit the rest.
# Quarantined files stay dirty, so the next run re-checks them and they
# sync themselves once clean. See agent/tools/scan_gate.py.
SCAN_GATE="$BRAIN_ROOT/tools/scan_gate.py"
QUARANTINE=""
if [ -n "$SCANNER" ] && [ -f "$SCAN_GATE" ]; then
    set +e
    QUARANTINE="$("$PYTHON_BIN" "$SCAN_GATE" --brain-root "$BRAIN_ROOT" \
                    --scanner "$SCANNER" 2>>"$LOG_FILE")"
    gate_rc=$?
    set -e
    # rc=2 means the scan itself could not run. An unscannable brain is
    # not a clean brain — fail closed, as before.
    if [ "$gate_rc" -eq 2 ]; then
        echo "$(date -u +%FT%TZ) sync: secret scan could not run; refusing to push" >> "$LOG_FILE"
        _refresh_pending_summary
        exit 1
    fi
elif [ -n "$SCANNER" ]; then
    # A scanner is installed but the gate that drives it is not — e.g. a
    # half-finished upgrade. Proceeding here would push the whole brain
    # with NO secret scan whatsoever, which is strictly worse than the
    # all-or-nothing behaviour this commit replaced. Fail closed.
    echo "$(date -u +%FT%TZ) sync: scan_gate.py missing at $SCAN_GATE; refusing to push" >> "$LOG_FILE"
    echo "$(date -u +%FT%TZ) sync: reinstall with ./install.sh to restore the secret gate" >> "$LOG_FILE"
    _refresh_pending_summary
    exit 2
fi

# ---- Stage all changes, minus anything quarantined ----
git add -A

if [ -n "$QUARANTINE" ]; then
    # An unborn HEAD (fresh brain, first ever sync) can't be reset against;
    # `git rm --cached` is the only way to unstage there.
    if git rev-parse --verify -q HEAD >/dev/null 2>&1; then
        HAVE_HEAD=1
    else
        HAVE_HEAD=0
    fi

    # Is exactly this path still in the index? `-z` avoids core.quotepath
    # mangling, and the :(literal) pathspec keeps the query exact.
    _is_staged() {
        local want="$1" got
        while IFS= read -r -d '' got; do
            [ "$got" = "$want" ] && return 0
        done < <(git diff --cached --name-only -z -- ":(literal)$want")
        return 1
    }

    # If the quarantined path is the DESTINATION of a staged rename, the
    # matching source deletion is a separate index entry. Unstaging only
    # the destination would commit the deletion while withholding the new
    # content — the remote would lose that memory entirely until the
    # quarantine clears. Restore the source too, so a rename+secret is a
    # clean no-op for this commit.
    #
    # `--name-status -z -M` emits R/C entries as three NUL fields
    # (status, old, new) and everything else as two.
    _restore_rename_source() {
        local want="$1" st old new
        while IFS= read -r -d '' st; do
            case "$st" in
                R*|C*)
                    IFS= read -r -d '' old || break
                    IFS= read -r -d '' new || break
                    if [ "$new" = "$want" ]; then
                        git reset -q HEAD -- ":(literal)$old" 2>>"$LOG_FILE" || true
                        echo "$(date -u +%FT%TZ) sync: also restored rename source of quarantined file: $old" >> "$LOG_FILE"
                    fi
                    ;;
                *)
                    IFS= read -r -d '' new || break
                    ;;
            esac
        done < <(git diff --cached --name-status -z -M)
    }

    N_QUARANTINED=0
    while IFS= read -r qfile; do
        [ -z "$qfile" ] && continue
        # Not in this commit anyway — gitignored, or unchanged since the
        # last sync. There is nothing to hold back, so don't count it and
        # don't raise a partial-sync warning that would never clear.
        if ! _is_staged "$qfile"; then
            continue
        fi
        # Leave the file dirty in the working tree — we only pull it out of
        # THIS commit, so the next run re-checks it and it syncs itself
        # once clean.
        #
        # :(literal) is load-bearing. Without it git treats the path as a
        # glob pathspec, so a note named `Meeting [2026-08-05].md` both
        # fails to match itself AND unstages every innocent file the
        # pattern happens to hit — silently withholding files we never
        # reported as quarantined.
        # Must run while the rename pairing is still staged.
        [ "$HAVE_HEAD" -eq 1 ] && _restore_rename_source "$qfile"
        if [ "$HAVE_HEAD" -eq 1 ]; then
            git reset -q HEAD -- ":(literal)$qfile" 2>>"$LOG_FILE" || true
        else
            git rm --cached -q --force -- ":(literal)$qfile" 2>>"$LOG_FILE" || true
        fi
        # Check the postcondition, not the exit code: `git reset` exits 0
        # even when the pathspec matched nothing, so its status proves
        # nothing. If the file is still staged, committing would publish
        # it — fail closed rather than trade a possible secret for a
        # successful sync.
        if _is_staged "$qfile"; then
            echo "$(date -u +%FT%TZ) sync: could not unstage $qfile; refusing to push" >> "$LOG_FILE"
            git reset -q 2>>"$LOG_FILE" || true
            _refresh_pending_summary
            exit 1
        fi
        N_QUARANTINED=$((N_QUARANTINED + 1))
        echo "$(date -u +%FT%TZ) sync: quarantined (not pushed): $qfile" >> "$LOG_FILE"
    done <<< "$QUARANTINE"
    # Only claim a partial sync if something was ACTUALLY held back. When
    # every reported path was skipped as unchanged or ignored, logging
    # "held back 0 file(s)" would pin render_pending_summary to the
    # 'quarantined' warning forever — it matches on the phrase alone.
    if [ "$N_QUARANTINED" -gt 0 ]; then
        echo "$(date -u +%FT%TZ) sync: held back $N_QUARANTINED file(s) with possible secrets; syncing the rest" >> "$LOG_FILE"
    fi
fi

# Anything to commit?
if git diff --cached --quiet; then
    echo "$(date -u +%FT%TZ) sync: no changes" >> "$LOG_FILE"
    # Refresh after writing the final log line so PENDING_REVIEW.md picks
    # up the new "ok" sync status — without this, a previous "blocked" /
    # "stale" warning persists until some other render trigger fires
    # (Codex 2026-05-05 P2).
    _refresh_pending_summary
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
