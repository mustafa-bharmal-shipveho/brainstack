#!/usr/bin/env bash
# brainstack installer for the global brain at ~/.agent/.
#
# Modes:
#   ./install.sh              -- install fresh ~/.agent/ if missing, else show status
#   ./install.sh --upgrade    -- refresh tools + hooks; leave memory/ untouched
#   ./install.sh --verify     -- self-check: confirm brain is healthy
#   ./install.sh --migrate <flat-memory-dir>
#                             -- run tools/migrate.py against the given dir
#
# Plug-in flags (work with any mode that creates a brain):
#   --brain-remote <url>      -- after install, init the brain as a git repo
#                                with this remote as origin (HTTPS or SSH).
#                                Also reads $BRAIN_REMOTE_URL if set.
#   --push-initial-commit     -- after --brain-remote, also push the first
#                                commit to the remote (skipped by default
#                                so the user can review locally first).
#   --brain-root <path>       -- override $BRAIN_ROOT for this run only.
#   --install-scanner [name]  -- attempt to install a secret scanner
#                                (default: trufflehog; pass `gitleaks` to
#                                use that instead). Skipped if already on
#                                PATH or no supported package manager.
#                                Currently supports macOS (brew) and Linux
#                                with apt/dnf/pacman.
#   --symlink-native          -- (DEFAULT, --migrate only) after migrating,
#                                replace the source dir with a symlink to
#                                $BRAIN_ROOT/memory so Claude Code's native
#                                writes flow into the brain. Original content
#                                is moved to <source>.bak.<unix-ts>.
#   --no-symlink              -- (--migrate only) leave the source dir in
#                                place after migration. Native writes will
#                                NOT reach the brain; you must set up your
#                                own forwarding to capture them.
#
# Always prints manual-merge instructions for ~/.claude/settings.json. The
# installer never auto-edits user settings — you copy the snippet by hand,
# preserving any other hooks/permissions you already have.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRAIN_ROOT="${BRAIN_ROOT:-$HOME/.agent}"
BRAIN_REMOTE="${BRAIN_REMOTE_URL:-}"
PUSH_INITIAL_COMMIT=0
INSTALL_SCANNER=""

MODE="install"
MIGRATE_SOURCE=""
# After a successful --migrate, replace the source dir with a symlink to
# $BRAIN_ROOT/memory so Claude Code's ongoing native writes flow into the
# brain instead of drifting away from it. Default ON; opt out with
# --no-symlink for users who want to keep their native dir untouched.
SYMLINK_NATIVE=1
# Track explicit user intent so we can refuse mutually exclusive flag pairs.
SYMLINK_NATIVE_FLAG_COUNT=0
# --dry-run shows the plan without executing; used by tests + cautious users.
DRY_RUN=0

# Captures any extra args after a mode flag so they can be forwarded to
# the relevant helper. Used for --setup-auto-migrate / --remove-auto-migrate
# which delegate to the Python helper for everything past the mode.
EXTRA_ARGS=()

while [ $# -gt 0 ]; do
    case "$1" in
        --upgrade) MODE="upgrade"; shift ;;
        --verify) MODE="verify"; shift ;;
        --setup-auto-migrate)
            MODE="setup-auto-migrate"
            shift
            # Everything else is forwarded to auto_migrate_install.py setup.
            # Note: global flags (--dry-run, --brain-root) parsed earlier
            # are also forwarded — see the call site for the merge logic.
            while [ $# -gt 0 ]; do EXTRA_ARGS+=("$1"); shift; done
            break
            ;;
        --remove-auto-migrate)
            MODE="remove-auto-migrate"
            shift
            while [ $# -gt 0 ]; do EXTRA_ARGS+=("$1"); shift; done
            break
            ;;
        --setup-claude-extras)
            MODE="setup-claude-extras"
            shift
            ;;
        --remove-claude-extras)
            MODE="remove-claude-extras"
            shift
            ;;
        --setup-cursor-rules)
            MODE="setup-cursor-rules"
            shift
            ;;
        --remove-cursor-rules)
            MODE="remove-cursor-rules"
            shift
            ;;
        --setup-shell-banner)
            MODE="setup-shell-banner"
            shift
            ;;
        --remove-shell-banner)
            MODE="remove-shell-banner"
            shift
            ;;
        --setup-pending-hook)
            MODE="setup-pending-hook"
            shift
            ;;
        --remove-pending-hook)
            MODE="remove-pending-hook"
            shift
            ;;
        --setup-statusline)
            MODE="setup-statusline"
            shift
            ;;
        --remove-statusline)
            MODE="remove-statusline"
            shift
            ;;
        --setup-codex-agents-md)
            MODE="setup-codex-agents-md"
            shift
            ;;
        --remove-codex-agents-md)
            MODE="remove-codex-agents-md"
            shift
            ;;
        --setup-pending-review-all)
            MODE="setup-pending-review-all"
            shift
            ;;
        --remove-pending-review-all)
            MODE="remove-pending-review-all"
            shift
            ;;
        --enable-auto-recall)
            MODE="enable-auto-recall"
            shift
            ;;
        --disable-auto-recall)
            MODE="disable-auto-recall"
            shift
            ;;
        --add-source)
            MODE="add-source"
            # Required: SRC path. Optional: --as DST_SUB on a later flag.
            if [ $# -lt 2 ] || [[ "$2" == --* ]]; then
                echo "install: --add-source requires a path" >&2
                exit 2
            fi
            ADD_SOURCE_PATH="$2"
            shift 2
            ;;
        --as)
            ADD_SOURCE_DST="${2:-}"
            if [ -z "$ADD_SOURCE_DST" ]; then
                echo "install: --as requires a destination subpath" >&2
                exit 2
            fi
            shift 2
            ;;
        --remove-source)
            MODE="remove-source"
            if [ $# -lt 2 ] || [[ "$2" == --* ]]; then
                echo "install: --remove-source requires a path or destination" >&2
                exit 2
            fi
            REMOVE_SOURCE_KEY="$2"
            shift 2
            ;;
        --list-sources)
            MODE="list-sources"
            shift
            ;;
        --migrate)
            MODE="migrate"
            # Consume the source path only if the next arg is NOT another
            # flag (handles `--migrate <path>`, `--migrate <path> --dry-run`,
            # `--migrate --dry-run`, and bare `--migrate` correctly).
            if [ $# -ge 2 ] && [[ "$2" != --* ]]; then
                MIGRATE_SOURCE="$2"
                shift 2
            else
                shift
            fi
            ;;
        --dry-run) DRY_RUN=1; shift ;;
        --symlink-native)
            SYMLINK_NATIVE=1
            SYMLINK_NATIVE_FLAG_COUNT=$((SYMLINK_NATIVE_FLAG_COUNT + 1))
            shift
            ;;
        --no-symlink)
            SYMLINK_NATIVE=0
            SYMLINK_NATIVE_FLAG_COUNT=$((SYMLINK_NATIVE_FLAG_COUNT + 1))
            shift
            ;;
        --brain-remote)
            BRAIN_REMOTE="${2:-}"
            if [ -z "$BRAIN_REMOTE" ]; then
                echo "install: --brain-remote requires a URL" >&2
                exit 2
            fi
            shift 2
            ;;
        --push-initial-commit)
            PUSH_INITIAL_COMMIT=1
            shift
            ;;
        --brain-root)
            BRAIN_ROOT="${2:-}"
            if [ -z "$BRAIN_ROOT" ]; then
                echo "install: --brain-root requires a path" >&2
                exit 2
            fi
            shift 2
            ;;
        --install-scanner)
            # Optional name argument: --install-scanner trufflehog | gitleaks
            # If next arg is missing or starts with `--`, default to trufflehog.
            if [ $# -ge 2 ] && [[ "$2" != --* ]]; then
                INSTALL_SCANNER="$2"
                shift 2
            else
                INSTALL_SCANNER="trufflehog"
                shift
            fi
            case "$INSTALL_SCANNER" in
                trufflehog|gitleaks) : ;;
                *)
                    echo "install: --install-scanner accepts only 'trufflehog' or 'gitleaks' (got: $INSTALL_SCANNER)" >&2
                    exit 2
                    ;;
            esac
            ;;
        --help|-h)
            sed -n '2,38p' "$0" | sed 's/^# //; s/^#//'
            exit 0
            ;;
        *)
            echo "install: unknown argument: $1" >&2
            echo "see ./install.sh --help" >&2
            exit 2
            ;;
    esac
done

# Mutually exclusive: passing both --symlink-native and --no-symlink is
# almost always a wrapper-script bug. Refuse rather than silently using
# whichever appeared last (per API persona finding #12).
if [ "$SYMLINK_NATIVE_FLAG_COUNT" -gt 1 ]; then
    echo "install: --symlink-native and --no-symlink are mutually exclusive" >&2
    exit 2
fi


# ----- Helper: install a secret scanner via the local package manager -----
maybe_install_scanner() {
    local name="$1"
    [ -z "$name" ] && return 0
    if command -v "$name" >/dev/null 2>&1; then
        echo "==> $name already on PATH; skipping install"
        return 0
    fi
    echo "==> Installing $name (this may take a minute)..."
    if command -v brew >/dev/null 2>&1; then
        # Homebrew works on macOS and Linuxbrew
        if [ "$name" = "trufflehog" ]; then
            brew install trufflesecurity/trufflehog/trufflehog
        else
            brew install "$name"
        fi
    elif command -v apt-get >/dev/null 2>&1; then
        # apt only carries gitleaks reliably; trufflehog needs a binary install
        if [ "$name" = "gitleaks" ]; then
            sudo apt-get update && sudo apt-get install -y gitleaks
        else
            echo "install: trufflehog isn't in apt; install via:" >&2
            echo "    curl -sSfL https://raw.githubusercontent.com/trufflesecurity/trufflehog/main/scripts/install.sh | sh -s -- -b /usr/local/bin" >&2
            return 1
        fi
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y "$name" || {
            echo "install: $name not found via dnf; install manually" >&2
            return 1
        }
    elif command -v pacman >/dev/null 2>&1; then
        sudo pacman -S --noconfirm "$name" || {
            echo "install: $name not found via pacman; install manually" >&2
            return 1
        }
    else
        echo "install: no supported package manager (brew/apt/dnf/pacman); install $name manually" >&2
        return 1
    fi
    if command -v "$name" >/dev/null 2>&1; then
        echo "==> $name installed: $(command -v "$name")"
    else
        echo "install: $name installation finished but binary not on PATH; check your shell rc" >&2
        return 1
    fi
}

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

# ----- Secret scanner check / optional install -----
# sync.sh fails closed without trufflehog or gitleaks. If --install-scanner
# was passed, attempt installation now; otherwise warn so the user can
# install before first sync.
if [ -n "$INSTALL_SCANNER" ]; then
    maybe_install_scanner "$INSTALL_SCANNER" || {
        echo "install: scanner install failed; sync.sh will refuse to push" >&2
        echo "         Set SYNC_ALLOW_NO_SCANNER=1 to bypass (NOT RECOMMENDED)." >&2
    }
elif ! command -v trufflehog >/dev/null 2>&1 && ! command -v gitleaks >/dev/null 2>&1; then
    echo "" >&2
    echo "install: WARNING - no secret scanner found on PATH." >&2
    echo "         sync.sh requires trufflehog or gitleaks to push." >&2
    echo "" >&2
    echo "         Install via this script:" >&2
    echo "           ./install.sh --install-scanner            # default: trufflehog" >&2
    echo "           ./install.sh --install-scanner gitleaks   # alternative" >&2
    echo "" >&2
    echo "         Or manually:" >&2
    echo "           brew install trufflehog        # or" >&2
    echo "           brew install gitleaks" >&2
    echo "" >&2
fi

# ----- Mode: migrate -----
if [ "$MODE" = "migrate" ]; then
    # No source path → drop into discovery + interactive flow.
    if [ -z "$MIGRATE_SOURCE" ]; then
        if [ "$DRY_RUN" = "1" ]; then
            echo "install: --dry-run requires a source path; pass --migrate <path> --dry-run, or omit --dry-run to run interactive discovery." >&2
            exit 2
        fi
        # Brain root must exist before discovery — discovery itself doesn't
        # write, but it's pointless without a target to migrate into.
        if [ ! -d "$BRAIN_ROOT" ]; then
            echo "install: $BRAIN_ROOT does not exist; run install first" >&2
            exit 2
        fi
        if [ ! -f "$BRAIN_ROOT/tools/migrate_dispatcher.py" ]; then
            echo "install: $BRAIN_ROOT/tools/migrate_dispatcher.py is missing" >&2
            echo "         run: ./install.sh --upgrade   to refresh tools" >&2
            exit 2
        fi
        BRAIN_ROOT="$BRAIN_ROOT" "$PYTHON_BIN" "$BRAIN_ROOT/tools/migrate_dispatcher.py" interactive
        exit $?
    fi
    # --dry-run with a source: run plan, write nothing.
    if [ "$DRY_RUN" = "1" ]; then
        if [ ! -e "$MIGRATE_SOURCE" ] && [ ! -L "$MIGRATE_SOURCE" ]; then
            echo "install: migrate source not found: $MIGRATE_SOURCE" >&2
            exit 2
        fi
        if [ ! -d "$BRAIN_ROOT" ]; then
            echo "install: $BRAIN_ROOT does not exist; run install first" >&2
            exit 2
        fi
        if [ ! -f "$BRAIN_ROOT/tools/migrate_dispatcher.py" ]; then
            echo "install: $BRAIN_ROOT/tools/migrate_dispatcher.py is missing" >&2
            echo "         run: ./install.sh --upgrade   to refresh tools" >&2
            exit 2
        fi
        BRAIN_ROOT="$BRAIN_ROOT" "$PYTHON_BIN" "$BRAIN_ROOT/tools/migrate_dispatcher.py" plan "$MIGRATE_SOURCE" "$BRAIN_ROOT"
        exit $?
    fi
    # Strip trailing slash. Shell completion happily appends one when the
    # arg is a directory; later `mv tmp_link "$MIGRATE_SOURCE"` would treat
    # a trailing-slash destination as "must be an existing dir" and fail
    # after we've already moved the source to backup.
    MIGRATE_SOURCE="${MIGRATE_SOURCE%/}"
    # Use -e (existence) so symlinks pass too; we distinguish symlink vs
    # regular dir below. The pair `-e + later -d` is intentional — see comment
    # at the bottom of this block.
    if [ ! -e "$MIGRATE_SOURCE" ] && [ ! -L "$MIGRATE_SOURCE" ]; then
        echo "install: migrate source not found: $MIGRATE_SOURCE" >&2
        exit 2
    fi
    if [ ! -d "$BRAIN_ROOT" ]; then
        echo "install: $BRAIN_ROOT does not exist; run install first" >&2
        exit 2
    fi
    # The brain memory dir must exist before we can compare a symlink against
    # it, AND `migrate.py` writes into it. Without this guard the idempotency
    # check could spuriously fall through (per reliability/security review).
    if [ ! -d "$BRAIN_ROOT/memory" ]; then
        echo "install: $BRAIN_ROOT/memory does not exist; run install first" >&2
        exit 2
    fi

    # Helper: canonical absolute path via Python's os.path.realpath. Handles
    # absolute and relative symlink targets, broken symlinks (returns the
    # input path unchanged for non-existent targets) and macOS BSD vs GNU
    # readlink differences. Echoes the resolved path to stdout, empty on
    # any error.
    resolve_real() {
        "$PYTHON_BIN" -c 'import os, sys
try:
    print(os.path.realpath(sys.argv[1]))
except Exception:
    pass' "$1" 2>/dev/null
    }

    brain_resolved="$(resolve_real "$BRAIN_ROOT/memory")"
    if [ -z "$brain_resolved" ]; then
        # `brain_resolved` is what we'll write into the symlink (when the
        # swap runs). It must be absolute regardless of how the user passed
        # `--brain-root` / `BRAIN_ROOT` (relative paths would create a
        # broken symlink resolved relative to the symlink's parent dir).
        echo "install: could not resolve $BRAIN_ROOT/memory to an absolute path" >&2
        exit 2
    fi

    if [ -L "$MIGRATE_SOURCE" ]; then
        link_resolved="$(resolve_real "$MIGRATE_SOURCE")"
        if [ -n "$link_resolved" ] && [ "$link_resolved" = "$brain_resolved" ]; then
            # Already pointing at the right place — true no-op.
            echo "==> $MIGRATE_SOURCE is already symlinked to $BRAIN_ROOT/memory — no-op"
            exit 0
        fi
        # Symlink exists but points elsewhere — refuse to silently overwrite
        # user-owned topology (per reliability persona finding #4 + security
        # finding #11). The user must remove the existing symlink themselves
        # if they really want to retarget it.
        echo "install: $MIGRATE_SOURCE is a symlink, but its target ($link_resolved)" >&2
        echo "         is not $BRAIN_ROOT/memory ($brain_resolved)." >&2
        echo "         Refusing to overwrite. Remove the symlink and re-run if intended." >&2
        exit 2
    fi

    # Not a symlink — must be a real directory at this point.
    if [ ! -d "$MIGRATE_SOURCE" ]; then
        echo "install: migrate source must be a directory: $MIGRATE_SOURCE" >&2
        exit 2
    fi

    echo "==> Migrating $MIGRATE_SOURCE -> $BRAIN_ROOT"
    # Detect format up front so we can route through the right path.
    # Per codex review of PR-B: previously this branch always invoked
    # migrate.py directly, which is the Claude-Code adapter — running it
    # on a Cursor or Codex source produced garbled output (Cursor plans
    # routed to personal/notes/ as generic Claude misc) AND would have
    # symlinked the native dir to the brain (wrong for Cursor/Codex,
    # whose tools keep writing to their own dirs).
    src_format="$("$PYTHON_BIN" "$BRAIN_ROOT/tools/migrate_dispatcher.py" plan "$MIGRATE_SOURCE" "$BRAIN_ROOT" 2>&1 | grep -E "^  Detected format:" | sed 's/^  Detected format: *//' | head -1)"

    case "$src_format" in
        claude-code-flat|claude-code-nested|claude-code-mixed)
            # Claude Code path: the legacy migrate.py invocation. After
            # success, the symlink swap below installs the native symlink
            # so Claude Code's ongoing auto-memory writes flow into the brain.
            if ! "$PYTHON_BIN" "$BRAIN_ROOT/tools/migrate.py" "$MIGRATE_SOURCE" "$BRAIN_ROOT"; then
                echo "install: migrate.py failed; not symlinking native dir" >&2
                echo "         migration data (if any wrote successfully) is in $BRAIN_ROOT/memory" >&2
                echo "         your source dir at $MIGRATE_SOURCE is unchanged" >&2
                echo "         tip: if this is a Python version error, retry with" >&2
                echo "              PYTHON_BIN=python3.13 ./install.sh --migrate $MIGRATE_SOURCE" >&2
                exit 1
            fi
            ;;
        *)
            # Non-Claude path: dispatcher routes to the right adapter
            # (Cursor today; PR-C's Codex adapter next). These tools
            # keep writing to their native dirs, so SYMLINK_NATIVE is
            # ignored — we ingest a snapshot only.
            if ! "$PYTHON_BIN" "$BRAIN_ROOT/tools/migrate_dispatcher.py" execute "$MIGRATE_SOURCE" "$BRAIN_ROOT"; then
                echo "install: dispatcher migration failed for format=$src_format" >&2
                echo "         your source dir at $MIGRATE_SOURCE is unchanged" >&2
                exit 1
            fi
            # Suppress the symlink swap below — it's Claude-only.
            SYMLINK_NATIVE=0
            echo "==> Non-Claude source ($src_format) migrated as a snapshot."
            echo "    Source dir at $MIGRATE_SOURCE left untouched. Re-run when"
            echo "    you want to import newer entries."
            ;;
    esac

    if [ "$SYMLINK_NATIVE" = "1" ]; then
        # Replace native source with a symlink to brain/memory so future
        # native writes (Claude Code, Cursor, etc.) flow into the brain.
        #
        # Atomic-ish symlink swap (per reliability + security review):
        #   1. Create the new symlink at a sibling temp name.
        #   2. Move the original source dir to the timestamped backup.
        #   3. Rename the temp symlink into the original source path.
        # If step 1 or 2 fails, the source is intact. If step 3 fails, the
        # backup retains the original data and the temp symlink is left for
        # the user to inspect — no silent data loss.
        #
        # `mktemp -d` would be cleaner for the backup name, but we want the
        # backup to live as a sibling of the source, and `mktemp -d` doesn't
        # accept a custom parent on all macOS versions reliably. Use a high-
        # resolution timestamp to avoid same-second collisions.
        ts="$(date +%s)"
        # Append PID + a short random tag to avoid same-second collisions
        # whether the backup-naming attacker is human, scripted, or just two
        # parallel migrations of separate dirs.
        rand="$RANDOM-$$"
        backup="${MIGRATE_SOURCE%/}.bak.$ts.$rand"
        if [ -e "$backup" ] || [ -L "$backup" ]; then
            echo "install: backup target $backup already exists; aborting" >&2
            exit 1
        fi
        tmp_link="${MIGRATE_SOURCE%/}.symlink-tmp.$ts.$rand"
        if [ -e "$tmp_link" ] || [ -L "$tmp_link" ]; then
            echo "install: temp symlink path $tmp_link already exists; aborting" >&2
            exit 1
        fi

        # Step 1 — make the new symlink at the temp name. Use the resolved
        # absolute path (computed above) so a relative `--brain-root` doesn't
        # produce a symlink whose target dangles relative to the source's
        # parent dir.
        if ! ln -s "$brain_resolved" "$tmp_link"; then
            echo "install: failed to create temp symlink at $tmp_link" >&2
            echo "         source dir at $MIGRATE_SOURCE is unchanged" >&2
            echo "         migration data is in $BRAIN_ROOT/memory" >&2
            exit 1
        fi
        # Step 2 — move the original source to the backup.
        if ! mv "$MIGRATE_SOURCE" "$backup"; then
            echo "install: failed to back up $MIGRATE_SOURCE -> $backup" >&2
            echo "         migration data is in $BRAIN_ROOT/memory; source is intact" >&2
            echo "         remove $tmp_link and re-run --migrate to retry the symlink swap" >&2
            rm -f "$tmp_link" 2>/dev/null || true
            exit 1
        fi
        # Step 3 — rename the temp symlink into the source position. If this
        # fails, the user is left with no source dir and no symlink; surface
        # the recovery path explicitly.
        if ! mv "$tmp_link" "$MIGRATE_SOURCE"; then
            echo "install: failed to install symlink at $MIGRATE_SOURCE" >&2
            echo "         source data is preserved at $backup" >&2
            echo "         temp symlink left at $tmp_link" >&2
            echo "         to recover manually: mv \"$backup\" \"$MIGRATE_SOURCE\"" >&2
            exit 1
        fi
        echo "==> Backed up original source -> $backup"
        echo "==> Symlinked $MIGRATE_SOURCE -> $BRAIN_ROOT/memory"
        echo "    Native auto-memory writes now flow into the brain."
    else
        echo "==> --no-symlink set; leaving $MIGRATE_SOURCE in place" >&2
        echo "==> WARNING: native writes after this point will NOT reach the brain." >&2
        echo "    Re-run with --symlink-native (or symlink manually) to capture them." >&2
    fi
    exit 0
fi

# ----- Mode: setup-auto-migrate / remove-auto-migrate -----
# Delegate to the Python helper. Forwards everything after the mode flag
# (--enable, --disable, --all, --none, --dry-run, --print-plist, etc.).
if [ "$MODE" = "setup-auto-migrate" ] || [ "$MODE" = "remove-auto-migrate" ]; then
    helper="$BRAIN_ROOT/tools/auto_migrate_install.py"
    if [ ! -f "$helper" ]; then
        echo "install: $helper is missing" >&2
        echo "         run: ./install.sh --upgrade   to refresh tools" >&2
        exit 2
    fi
    # Forward globally-parsed flags to the helper. `--dry-run` (parsed by
    # this wrapper) is the most important one — codex review P2 caught
    # `./install.sh --dry-run --setup-auto-migrate --all` performing a
    # real install because DRY_RUN never reached argparse.
    forwarded_args=()
    if [ "$DRY_RUN" = "1" ]; then
        forwarded_args+=(--dry-run)
    fi
    forwarded_args+=(--brain-root "$BRAIN_ROOT")
    # If the user ALSO passed --brain-root in the post-flag args, the
    # helper's argparse takes the LAST one, which is what they intended.
    # `${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}` guards against `set -u` tripping
    # on an empty array under macOS bash 3.2 — `--remove-auto-migrate` with
    # no trailing flags is the common case and used to error with
    # "EXTRA_ARGS[@]: unbound variable" before this guard.
    if [ "$MODE" = "setup-auto-migrate" ]; then
        BRAIN_ROOT="$BRAIN_ROOT" "$PYTHON_BIN" "$helper" setup "${forwarded_args[@]}" ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}
    else
        BRAIN_ROOT="$BRAIN_ROOT" "$PYTHON_BIN" "$helper" remove "${forwarded_args[@]}" ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}
    fi
    exit $?
fi

# ----- Mode: verify -----
if [ "$MODE" = "verify" ]; then
    echo "==> Verifying brain health at $BRAIN_ROOT"
    fail=0
    check() {
        local desc="$1"
        local cmd="$2"
        if eval "$cmd" >/dev/null 2>&1; then
            printf "  ✓ %s\n" "$desc"
        else
            printf "  ✗ %s\n" "$desc"
            fail=1
        fi
    }
    check "BRAIN_ROOT exists" "test -d '$BRAIN_ROOT'"
    check "memory/ layout present" "test -d '$BRAIN_ROOT/memory/episodic' -a -d '$BRAIN_ROOT/memory/working' -a -d '$BRAIN_ROOT/memory/candidates' -a -d '$BRAIN_ROOT/memory/semantic'"
    check "AGENT_LEARNINGS.jsonl present" "test -f '$BRAIN_ROOT/memory/episodic/AGENT_LEARNINGS.jsonl'"
    check "redact.py executable" "test -x '$BRAIN_ROOT/tools/redact.py' -o -f '$BRAIN_ROOT/tools/redact.py'"
    check "redact_jsonl.py present" "test -f '$BRAIN_ROOT/tools/redact_jsonl.py'"
    check "dream_runner.py present" "test -f '$BRAIN_ROOT/tools/dream_runner.py'"
    check "atomic helper present" "test -f '$BRAIN_ROOT/memory/_atomic.py'"
    check "global wrapper hook present" "test -f '$BRAIN_ROOT/harness/hooks/agentic_post_tool_global.py'"
    check "vendored hook present" "test -f '$BRAIN_ROOT/harness/hooks/claude_code_post_tool.py'"
    check "redact-private.txt present" "test -f '$BRAIN_ROOT/redact-private.txt'"

    # Optional but informative
    if command -v trufflehog >/dev/null 2>&1; then
        printf "  ✓ trufflehog on PATH\n"
    elif command -v gitleaks >/dev/null 2>&1; then
        printf "  ✓ gitleaks on PATH\n"
    else
        printf "  ⚠ no secret scanner on PATH (sync.sh will refuse to push)\n"
    fi

    # Run the redactor — exit 0 means clean
    if [ -f "$BRAIN_ROOT/tools/redact.py" ]; then
        if "$PYTHON_BIN" "$BRAIN_ROOT/tools/redact.py" "$BRAIN_ROOT" >/dev/null 2>&1; then
            printf "  ✓ brain scans clean\n"
        else
            printf "  ⚠ brain has redaction hits — run \`%s %s\` to see\n" \
                "$PYTHON_BIN" "$BRAIN_ROOT/tools/redact.py $BRAIN_ROOT"
        fi
    fi

    if [ "$fail" -eq 0 ]; then
        echo "==> Verify OK"
        exit 0
    else
        echo "==> Verify FAILED — see above" >&2
        exit 1
    fi
fi

# ----- Mode: upgrade -----
if [ "$MODE" = "upgrade" ]; then
    if [ ! -d "$BRAIN_ROOT" ]; then
        echo "install: $BRAIN_ROOT does not exist; nothing to upgrade. Run plain ./install.sh first." >&2
        exit 2
    fi
    echo "==> Upgrading code at $BRAIN_ROOT (memory user data left untouched)"
    # Convention: any file named `*.user.*` (e.g. tools/my_helper.user.sh) is
    # considered user-local and preserved across upgrades. Without this,
    # `rsync --delete` would silently remove user helpers on every upgrade.
    rsync -a --delete --exclude '__pycache__' --exclude '.pytest_cache' --exclude '*.pyc' \
        --exclude '*.user.*' \
        "$REPO_DIR/agent/tools/"   "$BRAIN_ROOT/tools/"
    rsync -a --delete --exclude '__pycache__' --exclude '.pytest_cache' --exclude '*.pyc' \
        --exclude '*.user.*' \
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
    # Pin the repo path so brain-side tools (check_freshness.py,
    # dream_runner.py, etc.) can later detect drift relative to the
    # exact repo this upgrade came from.
    echo "$REPO_DIR" > "$BRAIN_ROOT/.brainstack-repo-path"
    if [ -f "$BRAIN_ROOT/.gitignore" ] && \
       ! grep -qE "^\.brainstack-repo-path\s*$" "$BRAIN_ROOT/.gitignore"; then
        printf "\n# Repo path pin — machine-local, do not sync\n.brainstack-repo-path\n" \
            >> "$BRAIN_ROOT/.gitignore"
    fi
    # PENDING_REVIEW.md is regenerated locally on every dream/sync tick;
    # cross-machine sync would cause churn.
    if [ -f "$BRAIN_ROOT/.gitignore" ] && \
       ! grep -qE "^PENDING_REVIEW\.md\s*$" "$BRAIN_ROOT/.gitignore"; then
        printf "\n# Pending-review summary — regenerated locally\nPENDING_REVIEW.md\n" \
            >> "$BRAIN_ROOT/.gitignore"
    fi
    # Refresh the recall CLI symlink (idempotent; pip-installs into the venv
    # if the venv exists, otherwise creates it).
    if [ -x "$REPO_DIR/bin/install-recall-cli.sh" ]; then
        bash "$REPO_DIR/bin/install-recall-cli.sh" --quiet || true
    fi
    echo "==> Upgrade complete."
    exit 0
fi

# ----- Mode: setup-claude-extras / remove-claude-extras -----
# Installs the LaunchAgent that runs claude_session_adapter +
# claude_misc_adapter hourly. These adapters mirror Claude Code's
# session transcripts and misc dirs into the brain WITHOUT modifying
# the source — Claude keeps writing to ~/.claude/projects/ as normal,
# and brainstack pulls from there into ~/.agent/{memory/episodic/
# claude-sessions, imports/claude/}.
#
# Why a separate LaunchAgent (not the dispatcher's auto-migrate-all):
# the migrate_dispatcher only handles cursor-plans and codex-cli
# formats. The Claude session-transcript and misc-mirror adapters use
# their own incremental sidecars and don't fit that interface; running
# them from a sibling agent under the same fcntl lock keeps them from
# racing the dispatcher.
if [ "$MODE" = "setup-claude-extras" ] || [ "$MODE" = "remove-claude-extras" ]; then
    if [ ! -d "$BRAIN_ROOT" ]; then
        echo "install: $BRAIN_ROOT does not exist; run ./install.sh first." >&2
        exit 2
    fi
    plist_path="$HOME/Library/LaunchAgents/com.brainstack.claude-extras.plist"
    if [ "$MODE" = "remove-claude-extras" ]; then
        if [ -f "$plist_path" ]; then
            launchctl unload "$plist_path" 2>/dev/null || true
            rm -f "$plist_path"
            echo "==> com.brainstack.claude-extras LaunchAgent removed."
        else
            echo "==> Nothing to remove (no LaunchAgent at $plist_path)."
        fi
        exit 0
    fi

    # setup-claude-extras
    template="$REPO_DIR/templates/com.brainstack.claude-extras.plist"
    if [ ! -f "$template" ]; then
        echo "install: template missing: $template" >&2
        exit 2
    fi
    # Required adapter files must be in place. Without them the
    # LaunchAgent would just log errors hourly forever.
    for required in tools/sync_claude_extras.py tools/claude_session_adapter.py tools/claude_misc_adapter.py; do
        if [ ! -f "$BRAIN_ROOT/$required" ]; then
            echo "install: $BRAIN_ROOT/$required missing — run ./install.sh --upgrade first." >&2
            exit 2
        fi
    done
    mkdir -p "$BRAIN_ROOT/runtime/logs"
    # PYTHON_ABS is resolved earlier in install.sh from the venv. launchd
    # doesn't honor PATH, so this MUST be an absolute path. Codex
    # 2026-05-04 P1: previous template hard-coded the maintainer's path,
    # which broke setup on every other machine.
    if [ -z "${PYTHON_ABS:-}" ]; then
        echo "install: PYTHON_ABS not resolved — re-run plain ./install.sh first to bootstrap the venv." >&2
        exit 2
    fi
    sed -e "s|__BRAIN_ROOT__|$BRAIN_ROOT|g" \
        -e "s|__PYTHON_ABS__|$PYTHON_ABS|g" \
        "$template" > "$plist_path"
    # Validate before loading. plutil exits non-zero on malformed plists.
    if ! plutil -lint "$plist_path" >/dev/null 2>&1; then
        echo "install: rendered plist failed plutil --lint; not loading. See $plist_path" >&2
        exit 2
    fi
    launchctl unload "$plist_path" 2>/dev/null || true
    launchctl load "$plist_path"
    echo "==> com.brainstack.claude-extras LaunchAgent installed."
    echo "    plist:           $plist_path"
    echo "    runs every:      3600s"
    echo "    log:             $BRAIN_ROOT/claude-extras.log"
    echo "    Tear down with:  ./install.sh --remove-claude-extras"
    exit 0
fi

# ----- Mode: setup-cursor-rules / remove-cursor-rules -----
# Pushes <brain>/PENDING_REVIEW.md into ~/.cursor/.cursorrules between
# brainstack-pending-{start,end} sentinels so Cursor surfaces the
# pending-review summary on every chat session. Idempotent — re-running
# replaces the bracketed section without disturbing other content.
if [ "$MODE" = "setup-cursor-rules" ] || [ "$MODE" = "remove-cursor-rules" ]; then
    if [ ! -d "$BRAIN_ROOT" ]; then
        echo "install: $BRAIN_ROOT does not exist; run ./install.sh first." >&2
        exit 2
    fi
    cursor_dir="$HOME/.cursor"
    cursorrules="$cursor_dir/.cursorrules"
    if [ "$MODE" = "remove-cursor-rules" ]; then
        if [ -f "$cursorrules" ]; then
            "$PYTHON_BIN" - <<PYEOF
from pathlib import Path
p = Path("$cursorrules")
text = p.read_text()
START = "<!-- brainstack-pending-start -->"
END = "<!-- brainstack-pending-end -->"
if START in text and END in text:
    s = text.index(START)
    e = text.index(END) + len(END)
    new = text[:s].rstrip() + "\n" + text[e:].lstrip()
    p.write_text(new)
    print("removed brainstack section from", p)
else:
    print("no brainstack section found in", p)
PYEOF
        else
            echo "==> $cursorrules not found; nothing to remove."
        fi
        exit 0
    fi

    # setup-cursor-rules
    if [ ! -d "$cursor_dir" ]; then
        echo "install: $cursor_dir does not exist (Cursor not installed?). Skipping."
        exit 0
    fi
    # Generate the summary first if it doesn't exist
    if [ ! -f "$BRAIN_ROOT/PENDING_REVIEW.md" ]; then
        "$PYTHON_BIN" "$BRAIN_ROOT/tools/render_pending_summary.py" \
            --brain "$BRAIN_ROOT" 2>/dev/null || true
    fi
    "$PYTHON_BIN" "$BRAIN_ROOT/tools/render_cursor_rules.py" \
        --brain "$BRAIN_ROOT" --cursor-dir "$cursor_dir"
    echo "==> Cursor rules updated. Tear down with: ./install.sh --remove-cursor-rules"
    exit 0
fi

# ----- Mode: setup-shell-banner / remove-shell-banner -----
# Sources templates/brainstack-shell-banner.sh from the user's shell rc.
# Defines wrapper functions for `claude`, `codex`, `cursor` that print
# <brain>/PENDING_REVIEW.md before exec'ing the real binary. Idempotent
# (sentinel-marked block in the rc file).
if [ "$MODE" = "setup-shell-banner" ] || [ "$MODE" = "remove-shell-banner" ]; then
    if [ ! -d "$BRAIN_ROOT" ]; then
        echo "install: $BRAIN_ROOT does not exist; run ./install.sh first." >&2
        exit 2
    fi
    # Detect user's shell rc (zsh first; fall back to bash)
    if [ -n "${ZSH_VERSION:-}" ] || [ "$(basename "${SHELL:-/bin/zsh}")" = "zsh" ]; then
        rc="$HOME/.zshrc"
    else
        rc="$HOME/.bashrc"
    fi
    # Place the banner OUTSIDE $BRAIN_ROOT/tools/ — that path is
    # `rsync --delete`-managed by --upgrade, which would wipe the banner
    # on every upgrade and leave a dangling source line in ~/.zshrc
    # (Codex 2026-05-04 P2). $BRAIN_ROOT/banner/ is a sibling dir,
    # untouched by tool rsync.
    banner_dir="$BRAIN_ROOT/banner"
    banner_target="$banner_dir/brainstack-shell-banner.sh"
    sentinel_start="# >>> brainstack-shell-banner >>>"
    sentinel_end="# <<< brainstack-shell-banner <<<"

    if [ "$MODE" = "remove-shell-banner" ]; then
        if [ -f "$rc" ] && /usr/bin/grep -qF "$sentinel_start" "$rc"; then
            "$PYTHON_BIN" - <<PYEOF
from pathlib import Path
p = Path("$rc")
text = p.read_text()
S = "$sentinel_start"
E = "$sentinel_end"
if S in text and E in text:
    s = text.index(S)
    e = text.index(E) + len(E)
    # Trim surrounding whitespace
    new = text[:s].rstrip() + "\n" + text[e:].lstrip()
    if not new.endswith("\n"):
        new += "\n"
    p.write_text(new)
    print("removed brainstack-shell-banner block from", p)
PYEOF
        else
            echo "==> $rc has no brainstack-shell-banner block."
        fi
        rm -f "$banner_target"
        echo "==> Shell banner removed."
        exit 0
    fi

    # setup-shell-banner
    mkdir -p "$banner_dir"
    cp "$REPO_DIR/templates/brainstack-shell-banner.sh" "$banner_target"
    chmod +x "$banner_target"
    # Wrapped-tool list — config-driven so adding a new LLM is a one-line
    # edit, not a code change (Mustafa 2026-05-04: "framework, not point
    # solution"). Don't overwrite an existing user-curated list.
    wrapped_tools_target="$banner_dir/wrapped_tools"
    if [ ! -f "$wrapped_tools_target" ] && [ -f "$REPO_DIR/templates/brainstack-wrapped-tools.txt" ]; then
        cp "$REPO_DIR/templates/brainstack-wrapped-tools.txt" "$wrapped_tools_target"
        echo "==> Default wrapped-tool list seeded at $wrapped_tools_target"
        echo "    (edit to add/remove LLMs; re-source ~/.zshrc to apply)"
    fi
    if [ -f "$rc" ] && /usr/bin/grep -qF "$sentinel_start" "$rc"; then
        echo "==> $rc already sources the shell banner."
    else
        {
            echo ""
            echo "$sentinel_start"
            echo "[ -f \"$banner_target\" ] && source \"$banner_target\""
            echo "$sentinel_end"
        } >> "$rc"
        echo "==> Appended source line to $rc."
    fi
    echo "==> Shell banner installed. Run \`source $rc\` or open a new shell."
    echo "    Tear down with:  ./install.sh --remove-shell-banner"
    exit 0
fi

# ----- Mode: setup-pending-hook / remove-pending-hook -----
# Wires Claude Code to surface <brain>/PENDING_REVIEW.md at every
# session start by appending a sentinel-bracketed `@`-import to
# ~/.claude/CLAUDE.md.
#
# History: an earlier iteration of this mode registered a SessionStart
# hook in settings.json. That hook ran cleanly but Claude Code's
# SessionStart contract on this build is telemetry-only — stdout (raw
# OR JSON-enveloped) does NOT inject context. The user opened a fresh
# Claude session twice and saw nothing. Switched to CLAUDE.md @-import
# which IS the documented session-start injection mechanism (the user's
# CLAUDE.md already uses @-import for the org-level instructions).
#
# As a side effect, --remove-pending-hook also strips any leftover
# SessionStart entry tagged with our sentinel (cleans up post-upgrade
# from the prior iteration).
if [ "$MODE" = "setup-pending-hook" ] || [ "$MODE" = "remove-pending-hook" ]; then
    if [ ! -d "$BRAIN_ROOT" ]; then
        echo "install: $BRAIN_ROOT does not exist; run ./install.sh first." >&2
        exit 2
    fi
    claude_dir="$HOME/.claude"
    if [ ! -d "$claude_dir" ]; then
        echo "install: $claude_dir not found (Claude Code not installed?). Skipping."
        exit 0
    fi
    claude_md="$claude_dir/CLAUDE.md"
    settings="$claude_dir/settings.json"
    sentinel_start="<!-- brainstack-pending-review-start -->"
    sentinel_end="<!-- brainstack-pending-review-end -->"
    pending_path="$BRAIN_ROOT/PENDING_REVIEW.md"
    legacy_hook_sentinel="# brainstack-pending-review"

    if [ "$MODE" = "remove-pending-hook" ]; then
        # 1. Strip the @-import block from CLAUDE.md (current mechanism)
        if [ -f "$claude_md" ]; then
            "$PYTHON_BIN" - "$claude_md" "$sentinel_start" "$sentinel_end" <<'PYEOF'
import sys
from pathlib import Path
p, S, E = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
text = p.read_text()
if S in text and E in text:
    s = text.index(S)
    e = text.index(E) + len(E)
    new = text[:s].rstrip() + "\n" + text[e:].lstrip()
    if not new.endswith("\n"):
        new += "\n"
    p.write_text(new)
    print(f"removed brainstack-pending-review @import block from {p}")
else:
    print(f"no brainstack-pending-review block found in {p}")
PYEOF
        else
            echo "==> $claude_md not present; nothing to remove from CLAUDE.md."
        fi
        # 2. Also strip any leftover SessionStart hook entry from settings.json
        # (cleanup for users upgrading from the prior hook-based version)
        if [ -f "$settings" ]; then
            "$PYTHON_BIN" - "$settings" "$legacy_hook_sentinel" <<'PYEOF'
import json, sys
from pathlib import Path
p, sentinel = Path(sys.argv[1]), sys.argv[2]
try:
    data = json.loads(p.read_text())
except Exception:
    sys.exit(0)
hooks = data.get("hooks", {})
ss = hooks.get("SessionStart", []) or []
new_ss = []
removed = 0
for entry in ss:
    new_hooks = []
    for h in entry.get("hooks", []) or []:
        if sentinel in (h.get("command") or ""):
            removed += 1
            continue
        new_hooks.append(h)
    if new_hooks:
        e = dict(entry); e["hooks"] = new_hooks
        new_ss.append(e)
if removed:
    if new_ss:
        hooks["SessionStart"] = new_ss
    else:
        hooks.pop("SessionStart", None)
    data["hooks"] = hooks
    p.write_text(json.dumps(data, indent=2, sort_keys=True))
    print(f"removed {removed} legacy SessionStart hook(s) from {p}")
PYEOF
        fi
        exit 0
    fi

    # setup-pending-hook
    "$PYTHON_BIN" - "$claude_md" "$sentinel_start" "$sentinel_end" "$pending_path" <<'PYEOF'
import sys
from pathlib import Path
p, S, E, pending = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]

# The block we maintain inside CLAUDE.md. Absolute path inside the
# @-import — the @ handler may not expand $HOME / ~. Wrapped in a
# brief markdown stanza so a user reading their CLAUDE.md knows what
# the auto-loaded section is.
block = "\n".join([
    S,
    "## brainstack pending review",
    "",
    f"@{pending}",
    "",
    f"_Auto-loaded by brainstack. Remove with `./install.sh --remove-pending-hook`._",
    E,
])

if not p.is_file():
    p.write_text(block + "\n")
    print(f"created {p} with pending-review @-import")
    sys.exit(0)

text = p.read_text()
if S in text and E in text:
    # Already installed — replace in case the path changed (e.g., $BRAIN_ROOT moved)
    s = text.index(S)
    e = text.index(E) + len(E)
    new_text = text[:s] + block + text[e:]
    if new_text == text:
        print(f"already installed: {p} unchanged")
    else:
        p.write_text(new_text)
        print(f"updated brainstack-pending-review @-import in {p}")
else:
    # Append the block, preserving the user's existing content above
    sep = "" if text.endswith("\n") else "\n"
    if not text.endswith("\n\n"):
        sep += "\n"
    p.write_text(text + sep + block + "\n")
    print(f"appended brainstack-pending-review @-import to {p}")
PYEOF
    echo "==> CLAUDE.md @-import installed. Open a fresh Claude Code session to see it."
    echo "    The pending-review summary loads under the \"# claudeMd\" section of"
    echo "    every session's system prompt — same mechanism as your existing CLAUDE.md."
    echo "    Tear down with:  ./install.sh --remove-pending-hook"
    exit 0
fi

# ----- Mode: setup-statusline / remove-statusline -----
# Configures Claude Code's statusLine to run agent/tools/statusline.py
# which prints "📥 N pending — recall pending --review" in the persistent
# UI footer. Visible AS SOON AS the session opens, no user input required.
# Mustafa 2026-05-04: "can this happen when the user doesnt write anything
# and as soon as claude starts".
#
# Idempotent: re-runs replace the existing statusLine with our config
# (since settings.json supports only one statusLine; we own that slot).
# --remove-statusline restores no statusLine (Claude Code's default).
if [ "$MODE" = "setup-statusline" ] || [ "$MODE" = "remove-statusline" ]; then
    if [ ! -d "$BRAIN_ROOT" ]; then
        echo "install: $BRAIN_ROOT does not exist; run ./install.sh first." >&2
        exit 2
    fi
    settings="$HOME/.claude/settings.json"
    if [ ! -f "$settings" ]; then
        echo "install: $settings not found (Claude Code not installed?). Skipping."
        exit 0
    fi
    statusline_target="$BRAIN_ROOT/tools/statusline.py"

    if [ "$MODE" = "remove-statusline" ]; then
        "$PYTHON_BIN" - "$settings" "$statusline_target" <<'PYEOF'
import json, sys
from pathlib import Path
p, target = Path(sys.argv[1]), sys.argv[2]
data = json.loads(p.read_text())
sl = data.get("statusLine")
if isinstance(sl, dict) and target in str(sl.get("command", "")):
    data.pop("statusLine", None)
    p.write_text(json.dumps(data, indent=2, sort_keys=True))
    print(f"removed brainstack statusLine from {p}")
else:
    print(f"no brainstack statusLine found in {p}")
PYEOF
        exit 0
    fi

    # setup-statusline
    if [ ! -f "$statusline_target" ]; then
        echo "install: $statusline_target missing — run ./install.sh --upgrade first." >&2
        exit 2
    fi
    if [ -z "${PYTHON_ABS:-}" ]; then
        echo "install: PYTHON_ABS not resolved." >&2
        exit 2
    fi
    "$PYTHON_BIN" - "$settings" "$PYTHON_ABS" "$statusline_target" <<'PYEOF'
import json, sys
from pathlib import Path
settings_path, python_abs, target = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
data = json.loads(settings_path.read_text())
existing = data.get("statusLine")
if isinstance(existing, dict) and target in str(existing.get("command", "")):
    print(f"already installed: brainstack statusLine present in {settings_path}")
    sys.exit(0)
if existing:
    print(f"WARN: replacing existing statusLine in {settings_path} "
          f"(was: {existing.get('command', existing)!r})", file=sys.stderr)
data["statusLine"] = {
    "type": "command",
    "command": f"{python_abs} {target}",
}
settings_path.write_text(json.dumps(data, indent=2, sort_keys=True))
print(f"installed brainstack statusLine into {settings_path}")
PYEOF
    echo "==> Statusline installed. Open a fresh Claude Code session: the"
    echo "    pending count will appear in the UI footer immediately."
    echo "    Tear down with:  ./install.sh --remove-statusline"
    exit 0
fi

# ----- Mode: setup-codex-agents-md / remove-codex-agents-md -----
# Pushes <brain>/PENDING_REVIEW.md into ~/.codex/AGENTS.md between
# brainstack-pending-{start,end} sentinels. Codex CLI reads AGENTS.md
# at session start (similar to Claude reading CLAUDE.md). The directive
# at the top of PENDING_REVIEW.md tells Codex's AI to surface
# "brainstack: N pending - run `recall pending --review`" in its first
# response, same as Claude.
#
# Idempotent. Preserves user-authored AGENTS.md content above and below.
if [ "$MODE" = "setup-codex-agents-md" ] || [ "$MODE" = "remove-codex-agents-md" ]; then
    if [ ! -d "$BRAIN_ROOT" ]; then
        echo "install: $BRAIN_ROOT does not exist; run ./install.sh first." >&2
        exit 2
    fi
    codex_dir="$HOME/.codex"
    agents_md="$codex_dir/AGENTS.md"
    if [ "$MODE" = "remove-codex-agents-md" ]; then
        if [ -f "$agents_md" ]; then
            "$PYTHON_BIN" - <<PYEOF
from pathlib import Path
p = Path("$agents_md")
text = p.read_text()
START = "<!-- brainstack-pending-start -->"
END = "<!-- brainstack-pending-end -->"
if START in text and END in text:
    s = text.index(START)
    e = text.index(END) + len(END)
    new = text[:s].rstrip() + "\n" + text[e:].lstrip()
    if not new.endswith("\n"):
        new += "\n"
    p.write_text(new)
    print(f"removed brainstack section from {p}")
else:
    print(f"no brainstack section found in {p}")
PYEOF
        else
            echo "==> $agents_md not present; nothing to remove."
        fi
        exit 0
    fi

    # setup-codex-agents-md
    if [ ! -d "$codex_dir" ]; then
        echo "install: $codex_dir not found (Codex CLI not installed?). Skipping."
        exit 0
    fi
    if [ ! -f "$BRAIN_ROOT/PENDING_REVIEW.md" ]; then
        "$PYTHON_BIN" "$BRAIN_ROOT/tools/render_pending_summary.py" \
            --brain "$BRAIN_ROOT" 2>/dev/null || true
    fi
    "$PYTHON_BIN" "$BRAIN_ROOT/tools/render_codex_agents_md.py" \
        --brain "$BRAIN_ROOT" --codex-dir "$codex_dir"
    echo "==> Codex AGENTS.md updated. Open a fresh Codex CLI session: its AI"
    echo "    will greet you with the pending count on first response."
    echo "    Tear down with:  ./install.sh --remove-codex-agents-md"
    exit 0
fi

# ----- Mode: setup-pending-review-all / remove-pending-review-all -----
# Umbrella: wires all three pending-review surfaces in one command
# (Claude Code SessionStart hook + Cursor .cursorrules + shell wrappers).
# Each sub-mode is idempotent and silently skips its surface if the
# host tool isn't installed (e.g., no ~/.cursor → cursor step is no-op).
if [ "$MODE" = "setup-pending-review-all" ] || [ "$MODE" = "remove-pending-review-all" ]; then
    if [ ! -d "$BRAIN_ROOT" ]; then
        echo "install: $BRAIN_ROOT does not exist; run ./install.sh first." >&2
        exit 2
    fi
    if [ "$MODE" = "remove-pending-review-all" ]; then
        echo "==> Removing pending-review surfaces (statusline / Claude / Cursor / Codex / shell)…"
        "$0" --remove-statusline        2>&1 | sed 's/^/  /'
        "$0" --remove-pending-hook      2>&1 | sed 's/^/  /'
        "$0" --remove-cursor-rules      2>&1 | sed 's/^/  /'
        "$0" --remove-codex-agents-md   2>&1 | sed 's/^/  /'
        "$0" --remove-shell-banner      2>&1 | sed 's/^/  /'
        echo "==> Removal complete."
        exit 0
    fi

    # Setup all five. Pass PYTHON_BIN so each sub-call resolves the same
    # interpreter (avoids "system python3 too old" failures on default).
    # Order: statusline first (most user-visible: appears as soon as
    # session opens), then directives that fire on first response.
    echo "==> Setting up pending-review surfaces (statusline / Claude / Cursor / Codex / shell)…"
    PYTHON_BIN="$PYTHON_BIN" "$0" --setup-statusline        2>&1 | sed 's/^/  [statusln] /'
    PYTHON_BIN="$PYTHON_BIN" "$0" --setup-pending-hook      2>&1 | sed 's/^/  [claude]   /'
    PYTHON_BIN="$PYTHON_BIN" "$0" --setup-cursor-rules      2>&1 | sed 's/^/  [cursor]   /'
    PYTHON_BIN="$PYTHON_BIN" "$0" --setup-codex-agents-md   2>&1 | sed 's/^/  [codex]    /'
    PYTHON_BIN="$PYTHON_BIN" "$0" --setup-shell-banner      2>&1 | sed 's/^/  [shell]    /'
    echo
    echo "==> All five surfaces configured."
    echo "    Statusline:    Claude Code UI footer, visible immediately on session open"
    echo "    Claude greet:  via @-import in ~/.claude/CLAUDE.md"
    echo "    Cursor:        sentinel block in ~/.cursor/.cursorrules"
    echo "    Codex:         sentinel block in ~/.codex/AGENTS.md"
    echo "    Shell:         wrappers for any AI CLI in ~/.agent/banner/wrapped_tools"
    echo "    Tear down all: ./install.sh --remove-pending-review-all"
    exit 0
fi

# ----- Mode: add-source / remove-source / list-sources -----
# Manage extra import sources for the misc adapter. Each source is a directory
# (or single file) on disk that brainstack mirrors into <brain>/imports/<dst>/
# on every hourly LaunchAgent run. Use this to feed external knowledge bases,
# Obsidian vaults, or any locally-curated notes folder into the brain so it
# shows up in retrieval and survives across machines via the second-brain repo.
#
# Storage: <brain>/imports/extra_sources.txt — one "SRC=DST_SUB" line per
# source. Edit by hand if you prefer; these flags are just convenience.
if [ "$MODE" = "add-source" ] || [ "$MODE" = "remove-source" ] || [ "$MODE" = "list-sources" ]; then
    if [ ! -d "$BRAIN_ROOT" ]; then
        echo "install: $BRAIN_ROOT does not exist; run ./install.sh first." >&2
        exit 2
    fi
    EXTRA_SOURCES_FILE="$BRAIN_ROOT/imports/extra_sources.txt"
    mkdir -p "$BRAIN_ROOT/imports"

    if [ "$MODE" = "list-sources" ]; then
        echo "==> Configured extra sources (from $EXTRA_SOURCES_FILE):"
        if [ ! -f "$EXTRA_SOURCES_FILE" ]; then
            echo "    (none — file does not exist)"
            exit 0
        fi
        # Strip comments and blank lines for the listing
        /usr/bin/awk '!/^[[:space:]]*#/ && !/^[[:space:]]*$/ { print "    " $0 }' "$EXTRA_SOURCES_FILE"
        exit 0
    fi

    if [ "$MODE" = "add-source" ]; then
        # Resolve DST_SUB: explicit --as wins; otherwise derive from path basename.
        # Slug rules: lower-case, spaces → hyphens, drop non-alphanumeric (so
        # "Product & Tech KB" → "product-tech-kb"), collapse runs of hyphens,
        # trim leading/trailing hyphens.
        if [ -z "${ADD_SOURCE_DST:-}" ]; then
            base="$(/usr/bin/basename "$ADD_SOURCE_PATH")"
            slug="$(echo "$base" | /usr/bin/tr '[:upper:] ' '[:lower:]-' | /usr/bin/tr -cd '[:alnum:]-_/' | /usr/bin/sed -E 's/-+/-/g; s/^-//; s/-$//')"
            if [ -z "$slug" ]; then
                echo "ERROR: could not auto-derive a destination from $ADD_SOURCE_PATH (empty slug after sanitization)." >&2
                echo "       Pass an explicit --as <DST_SUB>." >&2
                exit 2
            fi
            ADD_SOURCE_DST="kb/$slug"
            echo "==> No --as given; using auto-derived destination: $ADD_SOURCE_DST"
        fi

        # Reject path-traversal in DST_SUB. The misc adapter joins this
        # directly under <brain>/imports/, so a value like "../../outside"
        # would silently write outside the brain on every hourly sync.
        # The misc adapter also rejects this at runtime, but failing fast
        # here gives the user a clear, immediate error.
        case "$ADD_SOURCE_DST" in
            /*|*/../*|*/..|../*|..)
                echo "ERROR: --as $ADD_SOURCE_DST is unsafe — must be a relative path under imports/, no '..' or leading '/'." >&2
                exit 2
                ;;
        esac
        if [ -z "$ADD_SOURCE_DST" ]; then
            echo "ERROR: --as cannot be empty." >&2
            exit 2
        fi

        # Validate: source must exist (warn but don't fail — user might be
        # registering ahead of creating the dir)
        if [ ! -e "$ADD_SOURCE_PATH" ] && [ ! -e "${ADD_SOURCE_PATH/#\~/$HOME}" ]; then
            echo "WARN: $ADD_SOURCE_PATH does not exist yet. Adding anyway; misc adapter will skip it until it appears." >&2
        fi

        # Normalize: if SRC is under $HOME, store as ~/<rel> so the file
        # stays readable + portable across machines with different $HOME.
        # This also makes idempotency work regardless of whether the user
        # typed `~/foo` (shell-expanded to absolute) or `$HOME/foo`.
        case "$ADD_SOURCE_PATH" in
            "$HOME"/*)
                ADD_SOURCE_PATH="~${ADD_SOURCE_PATH#$HOME}"
                ;;
            "$HOME")
                ADD_SOURCE_PATH="~"
                ;;
        esac

        # Initialize file with header if absent
        if [ ! -f "$EXTRA_SOURCES_FILE" ]; then
            cat > "$EXTRA_SOURCES_FILE" <<'HEADER'
# brainstack: extra import sources for the misc adapter.
#
# One entry per line: SRC=DST_SUB
#   - SRC may use ~ for $HOME (resolved at runtime).
#   - DST_SUB is the relative path under <brain>/imports/ where SRC is mirrored.
#   - Lines starting with # are comments. Blank lines are ignored.
#
# Manage with: ./install.sh --add-source / --remove-source / --list-sources
# Or edit this file directly. Changes are picked up on the next hourly sync.

HEADER
        fi

        # Idempotency: bail if SRC=DST already present
        new_line="$ADD_SOURCE_PATH=$ADD_SOURCE_DST"
        if /usr/bin/grep -Fxq "$new_line" "$EXTRA_SOURCES_FILE"; then
            echo "==> Source already registered: $new_line"
            exit 0
        fi
        # Bail if the SAME destination is already mapped (would cause overwrite).
        # Match on the literal "=$DST_SUB" suffix without using regex — paths
        # like "kb/[team]" or "kb/v1.2" would be misinterpreted as ERE patterns
        # by grep -E (Codex 2026-05-05 P2). Read the file line-by-line and
        # compare the post-'=' field as a string.
        existing=""
        while IFS= read -r _line; do
            case "$_line" in
                ""|"#"*) continue ;;
            esac
            _line_dst="${_line#*=}"
            if [ "$_line_dst" = "$ADD_SOURCE_DST" ]; then
                existing="$_line"
                break
            fi
        done < "$EXTRA_SOURCES_FILE"
        if [ -n "$existing" ]; then
            echo "ERROR: destination $ADD_SOURCE_DST already used by: $existing" >&2
            echo "       Pick a different --as value, or remove the existing entry first." >&2
            exit 2
        fi

        echo "$new_line" >> "$EXTRA_SOURCES_FILE"
        echo "==> Added: $new_line"
        echo "    Mirrored on next hourly sync to: $BRAIN_ROOT/imports/$ADD_SOURCE_DST"
        echo "    Backfill now:  $PYTHON_BIN $BRAIN_ROOT/tools/claude_misc_adapter.py"
        exit 0
    fi

    # remove-source: match either SRC path or DST_SUB as a LITERAL STRING.
    # We previously used `grep -E "(^KEY=|=KEY$)"`, but a key like
    # "kb/v1.2" or "kb/[team]" contains regex metacharacters that would
    # mis-match (Codex 2026-05-05 P2). Iterate the file line-by-line and
    # compare each side of the first '=' as a string.
    if [ ! -f "$EXTRA_SOURCES_FILE" ]; then
        echo "==> No extra sources configured (file does not exist)."
        exit 0
    fi
    matched=""
    tmpfile="$(mktemp)"
    while IFS= read -r _line || [ -n "$_line" ]; do
        # Always preserve comments + blank lines
        case "$_line" in
            ""|"#"*)
                printf '%s\n' "$_line" >> "$tmpfile"
                continue
                ;;
        esac
        _lhs="${_line%%=*}"
        _rhs="${_line#*=}"
        if [ "$_lhs" = "$REMOVE_SOURCE_KEY" ] || [ "$_rhs" = "$REMOVE_SOURCE_KEY" ]; then
            matched+="$_line"$'\n'
        else
            printf '%s\n' "$_line" >> "$tmpfile"
        fi
    done < "$EXTRA_SOURCES_FILE"

    if [ -z "$matched" ]; then
        rm -f "$tmpfile"
        echo "==> No source matching '$REMOVE_SOURCE_KEY' found. Run --list-sources to see registered entries." >&2
        exit 1
    fi
    mv "$tmpfile" "$EXTRA_SOURCES_FILE"
    echo "==> Removed entries matching '$REMOVE_SOURCE_KEY':"
    printf '%s' "$matched" | /usr/bin/sed 's/^/    /'
    echo "    Mirrored content under <brain>/imports/ is NOT deleted (could be intentional archive)."
    echo "    To delete: rm -rf $BRAIN_ROOT/imports/<DST_SUB>"
    exit 0
fi

# ----- Mode: enable-auto-recall / disable-auto-recall -----
# Toggle the per-prompt auto-recall feature. When enabled, every Claude
# Code user-prompt fires recall and injects top-K results as additional
# context via the existing UserPromptSubmit hook.
#
# This mode only flips the TOML flag. The UserPromptSubmit hook itself is
# already registered as part of the brainstack runtime install (verify by
# inspecting ~/.claude/settings.json — look for `runtime/adapters/claude_code/hooks.py`).
# If somehow it isn't, run --setup-claude-extras first.
if [ "$MODE" = "enable-auto-recall" ] || [ "$MODE" = "disable-auto-recall" ]; then
    if [ ! -d "$BRAIN_ROOT" ]; then
        echo "install: $BRAIN_ROOT does not exist; run ./install.sh first." >&2
        exit 2
    fi
    runtime_pyproject="$BRAIN_ROOT/runtime/pyproject.toml"
    mkdir -p "$BRAIN_ROOT/runtime"

    # Initialize the runtime pyproject.toml with the auto-recall section if
    # missing. Idempotent — Python helper reads + writes preserving any
    # other [tool.recall.runtime] keys the user may have set by hand.
    target_value="false"
    [ "$MODE" = "enable-auto-recall" ] && target_value="true"

    "$PYTHON_BIN" - "$runtime_pyproject" "$target_value" <<'PYEOF'
import re
import sys
from pathlib import Path

target = Path(sys.argv[1])
value = sys.argv[2]  # "true" or "false"

target.parent.mkdir(parents=True, exist_ok=True)
text = target.read_text() if target.is_file() else ""

# Ensure [tool.recall.runtime] section exists
if "[tool.recall.runtime]" not in text:
    sep = "\n\n" if text and not text.endswith("\n\n") else ""
    text += f"{sep}[tool.recall.runtime]\nlog_dir = \"~/.agent/runtime/logs\"\n"

# Patch or insert the enable_auto_recall key. Match within the
# [tool.recall.runtime] section only — don't accidentally hit a sibling
# section (e.g. [tool.recall.runtime.budget]) or a comment.
section_re = re.compile(
    r"(\[tool\.recall\.runtime\][^\[]*?)"  # group 1: section body
    r"(?=\[|\Z)",                           # until next section or EOF
    re.DOTALL,
)
m = section_re.search(text)
# We just ensured the section exists above, so the search always matches.
body = m.group(1)
key_re = re.compile(r"^enable_auto_recall\s*=.*$", re.MULTILINE)
if key_re.search(body):
    new_body = key_re.sub(f"enable_auto_recall = {value}", body)
else:
    # Append the key inside the section, preserving trailing whitespace
    new_body = body.rstrip("\n") + f"\nenable_auto_recall = {value}\n"
text = text[:m.start(1)] + new_body + text[m.end(1):]

target.write_text(text)
print(f"  enable_auto_recall = {value} written to {target}")
PYEOF

    if [ "$MODE" = "enable-auto-recall" ]; then
        echo "==> Auto-recall ENABLED."
        echo "    Every UserPromptSubmit hook fire will now query recall and inject top-K"
        echo "    results into Claude Code's context. Skip filter: prompts < 8 chars,"
        echo "    slash-commands, common acks (yes/ok/done)."
        echo ""
        echo "    See ROI:    recall stats --since 24h"
        echo "    Disable:    ./install.sh --disable-auto-recall"
    else
        echo "==> Auto-recall DISABLED."
        echo "    The UserPromptSubmit hook stays registered (other features may use it)."
        echo "    Re-enable with: ./install.sh --enable-auto-recall"
    fi
    exit 0
fi

# ----- Mode: install (default) -----
if [ -d "$BRAIN_ROOT" ]; then
    echo "==> $BRAIN_ROOT already exists. Status:"
    echo "    tools:    $(ls "$BRAIN_ROOT/tools" 2>/dev/null | wc -l | tr -d ' ') file(s)"
    echo "    hooks:    $(ls "$BRAIN_ROOT/harness/hooks" 2>/dev/null | wc -l | tr -d ' ') file(s)"
    echo "    memory:   $(find "$BRAIN_ROOT/memory" -type f 2>/dev/null | wc -l | tr -d ' ') file(s)"
    echo ""
    # Drift check: detect when the brain's framework code is older than
    # the repo we're invoked from. Common after `git pull` of brainstack
    # without re-running `./install.sh --upgrade`. Two real-world bugs
    # this catches: (1) `--setup-auto-migrate` failing because
    # auto_migrate_install.py wasn't seeded, (2) dream_runner.py running
    # a non-namespace-aware auto_dream.py and silently skipping
    # codex/claude-sessions episodes.
    # `[ -x ]` only resolves bare names like `python3` if they're a file
    # in CWD — useless. Use `command -v` so PATH lookup actually happens
    # (Codex 2026-05-04 P2). Falls back to PYTHON_ABS once it's set, but
    # we're in the no-flag status path here so PYTHON_ABS isn't computed
    # yet; resolve via PATH.
    _python_for_drift_check=""
    if command -v "$PYTHON_BIN" >/dev/null 2>&1; then
        _python_for_drift_check="$PYTHON_BIN"
    fi
    if [ -n "$_python_for_drift_check" ] && [ -f "$REPO_DIR/agent/tools/check_freshness.py" ]; then
        if ! "$_python_for_drift_check" "$REPO_DIR/agent/tools/check_freshness.py" \
                --repo "$REPO_DIR" --brain "$BRAIN_ROOT" --quiet; then
            echo ""
            echo "    ⚠️   Brain framework code is OUT OF SYNC with this repo."
            # NOTE: do NOT use backticks in echo — they trigger command
            # substitution and would actually execute --upgrade (Codex
            # 2026-05-04 P1). Single quotes keep the hint as literal text.
            echo "    Run './install.sh --upgrade' to refresh tools/, memory/, harness/"
            echo "    (no user data — episodic, candidates, semantic — is touched)."
            echo ""
        fi
    fi
    # Surface pending-review count if any candidates are waiting. Single
    # source of truth for the user's attention queue. Codex 2026-05-04 UX gap.
    if [ -f "$BRAIN_ROOT/PENDING_REVIEW.md" ]; then
        first_line="$(head -n 1 "$BRAIN_ROOT/PENDING_REVIEW.md" 2>/dev/null)"
        case "$first_line" in
            *"all clear"*) : ;;
            *)
                echo ""
                echo "    📥 Pending review: see $BRAIN_ROOT/PENDING_REVIEW.md"
                echo "       (run 'recall pending --review' or open Claude Code → /dream)"
                echo ""
                ;;
        esac
    fi
    echo "    To refresh tools/hooks without touching memory: ./install.sh --upgrade"
    echo "    To migrate a flat memory dir:                    ./install.sh --migrate <dir>"
    echo "    To enable hourly Claude transcript + misc sync:  ./install.sh --setup-claude-extras"
    echo "    To surface pending review on every session:      ./install.sh --setup-pending-review-all"
    echo "      (sets up Claude SessionStart + Cursor rules + shell wrappers)"
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

# Pin the repo path so brain-side tools (check_freshness.py, dream_runner,
# auto-migrate dispatcher) can later detect drift if the user `git pull`s
# the brainstack repo without re-running `./install.sh --upgrade`.
echo "$REPO_DIR" > "$BRAIN_ROOT/.brainstack-repo-path"
# Ensure the pin is gitignored even if the brain's .gitignore was created
# before this template line shipped. Idempotent. Codex 2026-05-04 P2.
if [ -f "$BRAIN_ROOT/.gitignore" ] && \
   ! grep -qE "^\.brainstack-repo-path\s*$" "$BRAIN_ROOT/.gitignore"; then
    printf "\n# Repo path pin — machine-local, do not sync\n.brainstack-repo-path\n" \
        >> "$BRAIN_ROOT/.gitignore"
fi
# PENDING_REVIEW.md is regenerated locally on every dream/sync tick.
if [ -f "$BRAIN_ROOT/.gitignore" ] && \
   ! grep -qE "^PENDING_REVIEW\.md\s*$" "$BRAIN_ROOT/.gitignore"; then
    printf "\n# Pending-review summary — regenerated locally\nPENDING_REVIEW.md\n" \
        >> "$BRAIN_ROOT/.gitignore"
fi

# Default .gitignore so the brain doesn't accidentally commit logs / lock
# files / temp files / dashboard exports. Mirrors the contents documented in
# docs/git-sync.md.
if [ ! -f "$BRAIN_ROOT/.gitignore" ] && [ -f "$REPO_DIR/templates/brain.gitignore" ]; then
    cp "$REPO_DIR/templates/brain.gitignore" "$BRAIN_ROOT/.gitignore"
fi

# Default trufflehog exclude so sync.sh's local scan skips .git/objects/
# (historical commits — already covered by server-side workflow + pre-commit).
if [ ! -f "$BRAIN_ROOT/.trufflehog-exclude.txt" ] && [ -f "$REPO_DIR/templates/trufflehog-exclude.txt" ]; then
    cp "$REPO_DIR/templates/trufflehog-exclude.txt" "$BRAIN_ROOT/.trufflehog-exclude.txt"
fi

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

# ----- Optional: --brain-remote auto-init -----
# If the user passed --brain-remote (or set BRAIN_REMOTE_URL), wire up the
# brain as a git repo with that remote and make an initial commit. Push only
# if --push-initial-commit was also passed.
if [ -n "$BRAIN_REMOTE" ]; then
    echo "==> Initializing brain as git repo with origin = $BRAIN_REMOTE"
    cd "$BRAIN_ROOT"
    if [ ! -d .git ]; then
        git init -q
        git branch -m main 2>/dev/null || true
    fi
    if git remote get-url origin >/dev/null 2>&1; then
        git remote set-url origin "$BRAIN_REMOTE"
    else
        git remote add origin "$BRAIN_REMOTE"
    fi
    # Stage + commit if there's anything to commit
    git add -A
    if ! git diff --cached --quiet; then
        git commit -q -m "Initial brain ($(date -u +%FT%TZ))"
        echo "    Initial commit created."
    fi
    # Install pre-commit hook automatically (it's defense-in-depth; cheap to add)
    if [ -f "$REPO_DIR/templates/pre-commit" ] && [ ! -f .git/hooks/pre-commit ]; then
        cp "$REPO_DIR/templates/pre-commit" .git/hooks/pre-commit
        chmod +x .git/hooks/pre-commit
        echo "    Pre-commit redaction hook installed."
    fi
    if [ "$PUSH_INITIAL_COMMIT" -eq 1 ]; then
        echo "==> Pushing initial commit"
        if git push -u origin main 2>&1 | tail -3; then
            echo "    Push complete."
        else
            echo "    Push failed — fix the issue and re-run \`git push -u origin main\`" >&2
        fi
    else
        echo "    Skipping push (use --push-initial-commit to push, or run"
        echo "    'cd $BRAIN_ROOT && git push -u origin main' when ready)."
    fi
    cd "$REPO_DIR"
fi

# Make `recall` available as a bare command (creates venv if missing,
# pip installs the package, symlinks into ~/.local/bin/). Idempotent.
if [ -x "$REPO_DIR/bin/install-recall-cli.sh" ]; then
    echo ""
    echo "==> Setting up the recall CLI on your PATH"
    bash "$REPO_DIR/bin/install-recall-cli.sh" || \
        echo "WARN: recall CLI symlink step failed; run \`bash $REPO_DIR/bin/install-recall-cli.sh\` manually." >&2
fi

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

  2. Initialize the brain as a git repo and add a private remote:

       cd $BRAIN_ROOT
       git init && git branch -m main
       git remote add origin <your-private-repo-url>
       git add . && git commit -m "Initial brain"
       git push -u origin main

     OR re-run the installer with --brain-remote to do this automatically:
       ./install.sh --brain-remote git@github.com:<you>/<your-brain-repo>.git \\
                    --push-initial-commit

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
