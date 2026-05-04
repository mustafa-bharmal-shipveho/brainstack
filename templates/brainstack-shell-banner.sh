#!/bin/bash
# brainstack-shell-banner.sh
#
# Sourced from ~/.zshrc / ~/.bashrc to define wrapper functions for
# `claude`, `codex`, `cursor` that print ~/.agent/PENDING_REVIEW.md
# before exec'ing the real binary.
#
# Each wrapper uses `command <tool>` (NOT bare `<tool>`). Without that
# prefix the function would call itself instead of the underlying binary,
# producing infinite recursion. This is the single most important contract
# in this file — tests pin it.
#
# Install via:  ./install.sh --setup-shell-banner
# Remove via:   ./install.sh --remove-shell-banner

_brainstack_print_pending() {
    local pending="${BRAIN_ROOT:-$HOME/.agent}/PENDING_REVIEW.md"
    if [ -f "$pending" ]; then
        # Skip the "all clear" one-liner (no noise on healthy days)
        local first_line
        first_line="$(/usr/bin/head -n 1 "$pending" 2>/dev/null)"
        case "$first_line" in
            *"all clear"*) : ;;
            *) /bin/cat "$pending" ;;
        esac
    fi
}

claude() {
    _brainstack_print_pending
    command claude "$@"
}

codex() {
    _brainstack_print_pending
    command codex "$@"
}

cursor() {
    _brainstack_print_pending
    command cursor "$@"
}
