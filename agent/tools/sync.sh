#!/usr/bin/env bash
# Sync the brain at $BRAIN_ROOT (default ~/.agent/) to its private remote.
#
# Behavior:
#   - flock on $BRAIN_ROOT/.brain.lock so we never run concurrent with the
#     dream cycle (auto_dream.py writes the same JSONL files we'd commit).
#   - exit 0 if there are no changes to commit.
#   - run trufflehog (if installed) on the brain dir; bail if it finds anything.
#   - run the redact pre-commit filter (already wired as a git hook).
#   - commit + push.
#
# Intended to be invoked by launchd hourly. See docs/git-sync.md.
set -euo pipefail

BRAIN_ROOT="${BRAIN_ROOT:-$HOME/.agent}"
LOCK_FILE="$BRAIN_ROOT/.brain.lock"
LOG_FILE="$BRAIN_ROOT/sync.log"

if [ ! -d "$BRAIN_ROOT" ]; then
    echo "$(date -u +%FT%TZ) sync: BRAIN_ROOT not found: $BRAIN_ROOT" >&2
    exit 0
fi

if [ ! -d "$BRAIN_ROOT/.git" ]; then
    echo "$(date -u +%FT%TZ) sync: $BRAIN_ROOT is not a git repo; nothing to sync" >&2
    exit 0
fi

# Acquire exclusive lock (non-blocking — if dream is running, skip this run)
exec 9> "$LOCK_FILE"
if ! flock -n 9; then
    echo "$(date -u +%FT%TZ) sync: another brain operation in progress, skipping" >> "$LOG_FILE"
    exit 0
fi

cd "$BRAIN_ROOT"

# Run trufflehog if available — entropy + token scanner. Don't push if it finds anything.
if command -v trufflehog >/dev/null 2>&1; then
    if ! trufflehog filesystem . --no-update --fail >/dev/null 2>>"$LOG_FILE"; then
        echo "$(date -u +%FT%TZ) sync: trufflehog flagged secrets; refusing to push" >> "$LOG_FILE"
        exit 1
    fi
fi

# Stage all changes
git add -A

# Anything to commit?
if git diff --cached --quiet; then
    # Nothing changed; record a heartbeat in the log and exit silently
    echo "$(date -u +%FT%TZ) sync: no changes" >> "$LOG_FILE"
    exit 0
fi

# Commit and push. The pre-commit hook (~/.agent/.git/hooks/pre-commit)
# runs redact.py and aborts if any pattern matches.
TS="$(date -u +%FT%TZ)"
if git commit -q -m "auto: $TS" 2>>"$LOG_FILE"; then
    if git push -q 2>>"$LOG_FILE"; then
        echo "$TS sync: pushed" >> "$LOG_FILE"
    else
        echo "$TS sync: commit succeeded but push failed; brain is committed locally" >> "$LOG_FILE"
        exit 1
    fi
else
    echo "$TS sync: commit blocked (likely by redact pre-commit hook)" >> "$LOG_FILE"
    exit 1
fi
