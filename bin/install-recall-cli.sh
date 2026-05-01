#!/usr/bin/env bash
# install-recall-cli.sh — make the `recall` command globally usable.
#
# Idempotent: re-running is a no-op when already set up. Three steps:
#   1. Create $REPO_DIR/.venv/ if missing.
#   2. pip install -e . into the venv (gets the recall + recall-mcp scripts
#      written into .venv/bin/).
#   3. Symlink .venv/bin/recall into ~/.local/bin/recall.
#
# Then prints PATH guidance if ~/.local/bin isn't already on $PATH.
#
# Usage:
#   bash bin/install-recall-cli.sh           # interactive install
#   bash bin/install-recall-cli.sh --quiet   # only emit warnings/errors
#
# Called automatically from install.sh on default install + --upgrade modes.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$REPO_DIR/.venv"
VENV_RECALL="$VENV_DIR/bin/recall"
TARGET_DIR="$HOME/.local/bin"
TARGET="$TARGET_DIR/recall"

QUIET=0
for arg in "$@"; do
    case "$arg" in
        --quiet|-q) QUIET=1 ;;
        --help|-h)
            grep -E '^#( |$)' "$0" | sed 's/^# \?//'
            exit 0
            ;;
    esac
done

log() { [ "$QUIET" -eq 0 ] && echo "==> $*"; }
warn() { echo "WARN: $*" >&2; }

# 1. venv
if [ ! -d "$VENV_DIR" ]; then
    PY="${PYTHON_BIN:-python3}"
    if ! command -v "$PY" >/dev/null 2>&1; then
        warn "$PY not found on PATH; cannot create venv"
        exit 1
    fi
    log "creating venv at $VENV_DIR (using $PY)"
    "$PY" -m venv "$VENV_DIR"
fi

# 2. pip install -e .
if [ ! -x "$VENV_RECALL" ]; then
    log "pip install -e . into $VENV_DIR"
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip >/dev/null
    "$VENV_DIR/bin/pip" install --quiet -e "$REPO_DIR"
fi

if [ ! -x "$VENV_RECALL" ]; then
    warn "pip install completed but $VENV_RECALL is still missing"
    exit 1
fi

# 3. symlink into ~/.local/bin
mkdir -p "$TARGET_DIR"
if [ -L "$TARGET" ]; then
    current="$(readlink "$TARGET")"
    if [ "$current" = "$VENV_RECALL" ]; then
        log "recall CLI already symlinked at $TARGET"
        SYMLINK_DONE=1
    else
        log "replacing stale symlink at $TARGET (was -> $current)"
        rm "$TARGET"
        ln -s "$VENV_RECALL" "$TARGET"
        SYMLINK_DONE=1
    fi
elif [ -e "$TARGET" ]; then
    backup="${TARGET}.bak.$(date +%s)"
    log "moving existing $TARGET to $backup before symlinking"
    mv "$TARGET" "$backup"
    ln -s "$VENV_RECALL" "$TARGET"
    SYMLINK_DONE=1
else
    ln -s "$VENV_RECALL" "$TARGET"
    log "symlinked $TARGET -> $VENV_RECALL"
    SYMLINK_DONE=1
fi

# 4. PATH check
case ":$PATH:" in
    *":$TARGET_DIR:"*)
        log "✓ \$HOME/.local/bin is already on your PATH; \`recall\` is ready to use"
        ;;
    *)
        if [ "$QUIET" -eq 0 ]; then
            cat <<EOF
==> NOTE: $TARGET_DIR is not on your PATH yet.
   Add this line to your shell rc (~/.zshrc or ~/.bashrc):
       export PATH="\$HOME/.local/bin:\$PATH"
   Then reload: source ~/.zshrc   (or open a new terminal)
EOF
        fi
        ;;
esac
