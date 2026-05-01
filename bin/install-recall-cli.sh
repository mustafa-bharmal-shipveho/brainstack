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

# 4. PATH check + optional interactive opt-in to add to shell rc.
#
# Without an interactive opt-in here, users who skim past the printed NOTE
# later type `recall remember "..."` and hit "command not found: recall".
# Default is NO (we don't auto-edit dotfiles); we offer the prompt only
# when running with a TTY. CI / dotfile bootstraps stay non-interactive.
PATH_LINE='export PATH="$HOME/.local/bin:$PATH"  # added by brainstack install-recall-cli.sh'
detect_rc() {
    # Return the user's primary shell rc path. Bash on macOS prefers
    # ~/.bash_profile (login shell); on Linux ~/.bashrc.
    case "${SHELL:-}" in
        */zsh) echo "$HOME/.zshrc" ;;
        */bash)
            if [ "$(uname -s)" = "Darwin" ] && [ -f "$HOME/.bash_profile" ]; then
                echo "$HOME/.bash_profile"
            else
                echo "$HOME/.bashrc"
            fi
            ;;
        *) echo "" ;;
    esac
}

case ":$PATH:" in
    *":$TARGET_DIR:"*)
        log "✓ \$HOME/.local/bin is already on your PATH; \`recall\` is ready to use"
        ;;
    *)
        if [ "$QUIET" -eq 1 ]; then
            : # quiet mode — don't print, don't prompt
        else
            cat <<EOF

==============================================================
PATH NOT SET: \`recall\` will be "command not found" until fixed.
==============================================================
$TARGET_DIR isn't on your \$PATH yet, so the symlink we just made
won't resolve from a fresh shell.
EOF
            RC="$(detect_rc)"
            if [ -n "$RC" ] && [ -t 0 ] && [ -t 1 ]; then
                # Idempotency — don't append twice if the user already added it.
                if [ -f "$RC" ] && grep -qE 'brainstack install-recall-cli\.sh|\.local/bin' "$RC" 2>/dev/null; then
                    cat <<EOF
$RC already references ~/.local/bin — looks like it was added previously.
Reload with: source $RC
EOF
                else
                    printf "\nAdd \`%s\` to %s? [y/N]: " 'export PATH="$HOME/.local/bin:$PATH"' "$RC"
                    read -r ANSWER
                    case "$ANSWER" in
                        y|Y|yes|YES)
                            # Append with a newline before for safety on a no-trailing-newline rc.
                            printf '\n%s\n' "$PATH_LINE" >> "$RC"
                            log "✓ Added the export line to $RC"
                            log "   Reload now with: source $RC   (or open a new terminal)"
                            ;;
                        *)
                            cat <<EOF
Skipped. To add it manually:
    echo '$PATH_LINE' >> $RC
    source $RC
EOF
                            ;;
                    esac
                fi
            else
                # Non-interactive (no TTY) or unknown shell — print copy-paste instructions.
                cat <<EOF
Add this line to your shell rc and reload:
    $PATH_LINE
EOF
            fi
        fi
        ;;
esac
