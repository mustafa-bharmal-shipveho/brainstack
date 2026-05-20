#!/usr/bin/env bash
# Convenience entry point: same as `./install.sh --uninstall "$@"`.
#
# Brainstack consolidates install/uninstall logic in install.sh so there's
# one source of truth for the surfaces it manages. This script just gives
# users a discoverable name (peer to install.sh) — `./uninstall.sh` and
# `./install.sh --uninstall` are interchangeable.
#
# Usage:
#   ./uninstall.sh                 # interactive, with confirmation
#   ./uninstall.sh -y              # skip the confirmation prompt
#   ./uninstall.sh --dry-run       # print the plan, change nothing
#   ./uninstall.sh --purge-data    # ALSO delete ~/.agent (explicit opt-in)
#
# See ./install.sh --help and the README "Uninstall" section for the full
# inventory of what is and isn't removed.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/install.sh" --uninstall "$@"
