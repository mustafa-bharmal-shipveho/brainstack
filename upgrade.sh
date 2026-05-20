#!/usr/bin/env bash
# Brainstack upgrade: one command that pulls the latest brainstack code and
# refreshes the installed brain framework + recall CLI.
#
# Behavior:
#   1. `git pull --ff-only` in the brainstack repo (the directory this
#      script lives in), so the repo is at the latest main commit.
#   2. exec `./install.sh --upgrade` to rsync the framework code into
#      ~/.agent/, refresh ~/.local/bin/recall, and preserve user data.
#
# Why auto-pull is the default: the alternative is a two-step flow
# (`cd ~/Documents/brainstack && git pull && ./install.sh --upgrade`)
# which colleagues forget — they stay on stale versions for weeks.
#
# Usage:
#   ./upgrade.sh             # default: git pull --ff-only, then upgrade
#   ./upgrade.sh --no-pull   # skip the pull (you manage git yourself)
#   ./upgrade.sh --help      # print this header
#
# Opt out with `--no-pull` if you already pulled OR you're in a CI/release
# context that handles git separately. `--no-pull` matches the legacy
# behavior of "just refresh the brain from whatever's checked out."

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DO_PULL=1
EXTRA_ARGS=()
for arg in "$@"; do
    case "$arg" in
        --no-pull)
            DO_PULL=0
            ;;
        --help|-h)
            grep -E '^#( |$)' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            EXTRA_ARGS+=("$arg")
            ;;
    esac
done

if [ "$DO_PULL" = "1" ]; then
    if [ ! -d "$SCRIPT_DIR/.git" ]; then
        echo "upgrade: $SCRIPT_DIR is not a git repo; cannot auto-pull. Use --no-pull." >&2
        exit 2
    fi
    echo "==> Pulling latest brainstack code from origin/main"
    # --ff-only refuses non-fast-forward merges; tells the user to handle
    # divergence themselves rather than silently merging.
    if ! git -C "$SCRIPT_DIR" pull --ff-only origin main 2>&1; then
        echo "" >&2
        echo "upgrade: git pull failed (likely diverged from origin/main)." >&2
        echo "         Resolve manually, then re-run:" >&2
        echo "           ./upgrade.sh --no-pull" >&2
        exit 1
    fi
    echo ""
fi

# `${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}` is the bash idiom for "expand the
# array if set, else expand to nothing" — safe under `set -u`.
exec "$SCRIPT_DIR/install.sh" --upgrade ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}
