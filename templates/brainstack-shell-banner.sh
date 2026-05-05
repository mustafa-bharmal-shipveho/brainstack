#!/bin/bash
# brainstack-shell-banner.sh
#
# Sourced from ~/.zshrc / ~/.bashrc. Defines wrapper functions for any
# AI-CLI binary on $PATH so each one prints ~/.agent/PENDING_REVIEW.md
# before exec'ing the real tool. Design goal (Mustafa 2026-05-04):
# **framework, not point-solution.** Adding a new LLM is a config-line
# edit, not a code change.
#
# How it works
# ------------
#
# The wrapped-tool list comes from ~/.agent/banner/wrapped_tools (one
# tool name per line, # comments allowed). Each name in that list gets
# a wrapper function defined dynamically here. The wrappers use
# `command <tool> "$@"` (NOT bare `<tool>`) — without that prefix the
# function calls itself instead of the underlying binary, infinitely.
# That's the single most important contract; tests pin it.
#
# To add a new tool (e.g. `aider`): add the line `aider` to
# ~/.agent/banner/wrapped_tools and re-source this file.
#
# Install:  ./install.sh --setup-shell-banner
# Remove:   ./install.sh --remove-shell-banner

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

# Read the wrapped-tool list. Skip blank lines and # comments. If the
# config file is missing, fall back to the canonical default set so a
# fresh install doesn't silently no-op.
_brainstack_load_wrapped_tools() {
    local config="${BRAIN_ROOT:-$HOME/.agent}/banner/wrapped_tools"
    if [ -f "$config" ]; then
        /usr/bin/awk '!/^[[:space:]]*#/ && !/^[[:space:]]*$/ { gsub(/[[:space:]]/, ""); print }' "$config"
    else
        # Default set — matches what most users will have
        echo "claude"
        echo "codex"
        echo "cursor"
    fi
}

# Define one wrapper function per tool in the config. Using `eval` is the
# only way to dynamically create a function with a known name in bash.
# Each wrapper:
#   1. Prints PENDING_REVIEW.md content (suppressed if "all clear")
#   2. Exec's the real binary via `command <tool> "$@"`
# The `command` prefix is mandatory — bare `claude "$@"` would recurse.
while IFS= read -r _bs_tool; do
    [ -z "$_bs_tool" ] && continue
    eval "
${_bs_tool}() {
    _brainstack_print_pending
    command ${_bs_tool} \"\$@\"
}
"
done < <(_brainstack_load_wrapped_tools)

unset _bs_tool
