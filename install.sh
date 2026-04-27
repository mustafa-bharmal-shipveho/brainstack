#!/usr/bin/env bash
# mustafa-agentic-stack installer for the global brain at ~/.agent/.
#
# Modes:
#   ./install.sh              -- install fresh ~/.agent/ if missing, else show status
#   ./install.sh --upgrade    -- refresh tools + hooks; leave memory/ untouched
#   ./install.sh --migrate <flat-memory-dir>
#                             -- run tools/migrate.py against the given dir
#
# Always prints manual-merge instructions for ~/.claude/settings.json. The
# installer never auto-edits user settings — you copy the snippet by hand,
# preserving any other hooks/permissions you already have.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRAIN_ROOT="${BRAIN_ROOT:-$HOME/.agent}"

MODE="install"
MIGRATE_SOURCE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --upgrade) MODE="upgrade"; shift ;;
        --migrate)
            MODE="migrate"
            MIGRATE_SOURCE="${2:-}"
            shift 2 || true
            ;;
        --help|-h)
            sed -n '2,12p' "$0" | sed 's/^# //; s/^#//'
            exit 0
            ;;
        *)
            echo "install: unknown argument: $1" >&2
            echo "see ./install.sh --help" >&2
            exit 2
            ;;
    esac
done

# ----- Python version check -----
PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "install: $PYTHON_BIN not found in PATH" >&2
    exit 1
fi

PY_VER="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PY_MAJOR="$("$PYTHON_BIN" -c 'import sys; print(sys.version_info.major)')"
PY_MINOR="$("$PYTHON_BIN" -c 'import sys; print(sys.version_info.minor)')"

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    echo "install: Python >= 3.10 required (found $PY_VER at $($PYTHON_BIN -c 'import sys;print(sys.executable)'))" >&2
    echo ""
    echo "On macOS:  brew install python@3.13" >&2
    echo "Then:      PYTHON_BIN=python3.13 ./install.sh" >&2
    exit 1
fi

# Resolve absolute path so launchd plists / hook commands don't depend on PATH.
PYTHON_ABS="$("$PYTHON_BIN" -c 'import sys; print(sys.executable)')"

# ----- Secret scanner check -----
# sync.sh fails closed without trufflehog or gitleaks. Warn here so the user
# can install one before first sync attempt.
if ! command -v trufflehog >/dev/null 2>&1 && ! command -v gitleaks >/dev/null 2>&1; then
    echo "" >&2
    echo "install: WARNING — no secret scanner found on PATH." >&2
    echo "         sync.sh requires trufflehog or gitleaks to push." >&2
    echo "" >&2
    echo "         Install one before first sync:" >&2
    echo "           brew install trufflehog        # or" >&2
    echo "           brew install gitleaks" >&2
    echo "" >&2
fi

# ----- Mode: migrate -----
if [ "$MODE" = "migrate" ]; then
    if [ -z "$MIGRATE_SOURCE" ]; then
        echo "install: --migrate requires a source directory" >&2
        exit 2
    fi
    if [ ! -d "$MIGRATE_SOURCE" ]; then
        echo "install: migrate source not found: $MIGRATE_SOURCE" >&2
        exit 2
    fi
    if [ ! -d "$BRAIN_ROOT" ]; then
        echo "install: $BRAIN_ROOT does not exist; run install first" >&2
        exit 2
    fi
    echo "==> Migrating $MIGRATE_SOURCE -> $BRAIN_ROOT"
    "$PYTHON_BIN" "$BRAIN_ROOT/tools/migrate.py" "$MIGRATE_SOURCE" "$BRAIN_ROOT"
    exit $?
fi

# ----- Mode: upgrade -----
if [ "$MODE" = "upgrade" ]; then
    if [ ! -d "$BRAIN_ROOT" ]; then
        echo "install: $BRAIN_ROOT does not exist; nothing to upgrade. Run plain ./install.sh first." >&2
        exit 2
    fi
    echo "==> Upgrading code at $BRAIN_ROOT (memory user data left untouched)"
    rsync -a --delete --exclude '__pycache__' --exclude '.pytest_cache' --exclude '*.pyc' \
        "$REPO_DIR/agent/tools/"   "$BRAIN_ROOT/tools/"
    rsync -a --delete --exclude '__pycache__' --exclude '.pytest_cache' --exclude '*.pyc' \
        "$REPO_DIR/agent/harness/" "$BRAIN_ROOT/harness/"
    # memory/ holds BOTH framework code (*.py) and user data (working/, episodic/,
    # candidates/, personal/, semantic/, *.md). Sync only the framework Python
    # files individually so user data stays put.
    # mkdir -p first so a partial brain (where memory/ doesn't exist yet)
    # doesn't break the cp loop with "No such file or directory".
    mkdir -p "$BRAIN_ROOT/memory"
    for src in "$REPO_DIR/agent/memory/"*.py; do
        [ -f "$src" ] || continue
        cp -f "$src" "$BRAIN_ROOT/memory/$(basename "$src")"
    done
    chmod +x "$BRAIN_ROOT/tools/"*.sh "$BRAIN_ROOT/tools/"*.py 2>/dev/null || true
    chmod +x "$BRAIN_ROOT/harness/hooks/"*.py 2>/dev/null || true
    chmod +x "$BRAIN_ROOT/memory/"*.py 2>/dev/null || true
    echo "==> Upgrade complete."
    exit 0
fi

# ----- Mode: install (default) -----
if [ -d "$BRAIN_ROOT" ]; then
    echo "==> $BRAIN_ROOT already exists. Status:"
    echo "    tools:    $(ls "$BRAIN_ROOT/tools" 2>/dev/null | wc -l | tr -d ' ') file(s)"
    echo "    hooks:    $(ls "$BRAIN_ROOT/harness/hooks" 2>/dev/null | wc -l | tr -d ' ') file(s)"
    echo "    memory:   $(find "$BRAIN_ROOT/memory" -type f 2>/dev/null | wc -l | tr -d ' ') file(s)"
    echo ""
    echo "    To refresh tools/hooks without touching memory: ./install.sh --upgrade"
    echo "    To migrate a flat memory dir:                    ./install.sh --migrate <dir>"
    exit 0
fi

echo "==> Installing brain at $BRAIN_ROOT"
mkdir -p "$BRAIN_ROOT"

# Copy the agent/ tree
rsync -a --exclude '__pycache__' --exclude '.pytest_cache' --exclude '*.pyc' "$REPO_DIR/agent/" "$BRAIN_ROOT/"

# Seed empty memory layers
mkdir -p "$BRAIN_ROOT/memory/working"
mkdir -p "$BRAIN_ROOT/memory/episodic"
mkdir -p "$BRAIN_ROOT/memory/semantic/lessons"
mkdir -p "$BRAIN_ROOT/memory/personal/profile"
mkdir -p "$BRAIN_ROOT/memory/personal/notes"
mkdir -p "$BRAIN_ROOT/memory/personal/references"
mkdir -p "$BRAIN_ROOT/memory/candidates"

# Empty episodic JSONL (touched so the hook can append)
touch "$BRAIN_ROOT/memory/episodic/AGENT_LEARNINGS.jsonl"

# Permissions
chmod +x "$BRAIN_ROOT/tools/"*.sh 2>/dev/null || true
chmod +x "$BRAIN_ROOT/tools/"*.py 2>/dev/null || true
chmod +x "$BRAIN_ROOT/harness/hooks/"*.py 2>/dev/null || true

# Stub for private redaction patterns (lives in user's brain, not in framework).
# Empty by default; the user can copy templates/redact-private.example.txt over
# to seed common org-shape regexes.
cat > "$BRAIN_ROOT/redact-private.txt" <<'EOF'
# Private redaction allowlist + extra patterns.
# This file is local to your brain — never committed to the public framework.
# One regex per line. Patterns are merged into the public BUILTIN_PATTERNS
# at scan time. Patterns with ReDoS-prone nested quantifiers are rejected.
#
# For a starting set of org-PII shapes:
#   cp $REPO_DIR/templates/redact-private.example.txt $BRAIN_ROOT/redact-private.txt
EOF

cat <<EOF

==> Installed. Brain is at: $BRAIN_ROOT

Next steps (manual — installer never edits ~/.claude/ for safety):

  1. Add the global hook to ~/.claude/settings.json. The snippet template
     uses placeholders — fill in the absolute paths shown below (do NOT use
     \$HOME or ~ in the command; absolute paths defend against env-poisoning):

       Python interpreter: $PYTHON_ABS
       Hook wrapper:       $BRAIN_ROOT/harness/hooks/agentic_post_tool_global.py

       So the hook entry is:
         "command": "$PYTHON_ABS $BRAIN_ROOT/harness/hooks/agentic_post_tool_global.py"

     Reference template:
       cat $REPO_DIR/adapters/claude-code/settings.snippet.json

     Merge into your settings.json under "hooks.PostToolUse". Validate:
       python3 -m json.tool ~/.claude/settings.json > /dev/null

  2. Initialize ~/.agent/ as a git repo and add a private remote:

       cd $BRAIN_ROOT
       git init && git branch -m main
       git remote add origin <your-private-repo-url>
       git add . && git commit -m "Initial brain"
       git push -u origin main

  3. Set up nightly dream + hourly sync via launchd:
       cp $REPO_DIR/templates/com.user.agent-dream.plist ~/Library/LaunchAgents/
       cp $REPO_DIR/templates/com.user.agent-sync.plist ~/Library/LaunchAgents/
       launchctl load ~/Library/LaunchAgents/com.user.agent-dream.plist
       launchctl load ~/Library/LaunchAgents/com.user.agent-sync.plist

  4. (Optional) Migrate an existing flat memory directory:
       $REPO_DIR/install.sh --migrate ~/.claude/projects/<slug>/memory

  5. (Recommended) Pre-commit hook for secret scanning:
       cd $BRAIN_ROOT
       cp $REPO_DIR/templates/pre-commit .git/hooks/pre-commit
       chmod +x .git/hooks/pre-commit

  6. (Recommended) Server-side secret scan workflow on the brain repo
     (catches \`git commit --no-verify\` bypass attempts):
       mkdir -p $BRAIN_ROOT/.github/workflows
       cp $REPO_DIR/templates/brain-secret-scan.yml \\
          $BRAIN_ROOT/.github/workflows/secret-scan.yml

See docs/claude-code-setup.md and docs/git-sync.md for details.
EOF
