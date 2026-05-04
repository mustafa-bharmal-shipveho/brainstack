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
    echo "    To refresh tools/hooks without touching memory: ./install.sh --upgrade"
    echo "    To migrate a flat memory dir:                    ./install.sh --migrate <dir>"
    echo "    To enable hourly Claude transcript + misc sync:  ./install.sh --setup-claude-extras"
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
