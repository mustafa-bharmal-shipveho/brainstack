#!/usr/bin/env python3
"""Global Claude Code PostToolUse hook with precedence logic.

This is the entry point Claude Code invokes per tool call. It:

  1. Resolves the target brain location:
     - If BRAIN_ROOT env var is set and non-empty → use that
     - Else → ~/.agent/
  2. Checks for `.agent-local-override` in $CLAUDE_PROJECT_DIR.
     If present, exits 0 immediately. Use case: the project has its own
     upstream-agentic-stack `.agent/` folder with its own hooks, and we
     don't want double-logging.
  3. Otherwise, dispatches to the vendored claude_code_post_tool.py with
     AGENT_ROOT pointing at the resolved brain. Reads the same JSON payload
     from stdin and forwards it.

Always exits 0 (a hook failure shouldn't break Claude Code's tool flow).
"""
import json
import os
import subprocess
import sys
from pathlib import Path


def resolve_brain_root() -> Path:
    """Pick the brain location based on env precedence."""
    explicit = os.environ.get("BRAIN_ROOT", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return Path(os.path.expanduser("~/.agent"))


def has_local_override() -> bool:
    """True if $CLAUDE_PROJECT_DIR/.agent-local-override exists."""
    project = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    if not project:
        return False
    marker = Path(project) / ".agent-local-override"
    return marker.exists()


def main() -> int:
    if has_local_override():
        # Project's own hooks handle this; skip silently.
        return 0

    brain = resolve_brain_root()
    vendor_hook = brain / "harness" / "hooks" / "claude_code_post_tool.py"

    if not vendor_hook.exists():
        # Brain not installed yet, or path mis-resolved. Don't error;
        # Claude Code's tool flow shouldn't break because the hook is missing.
        return 0

    # Forward stdin to the vendored hook. The vendored hook reads tool_name /
    # tool_input / tool_response from stdin and resolves AGENT_ROOT from its
    # own __file__ location — no env var needed since we pass the absolute
    # path to the vendored script under the resolved brain.
    payload = sys.stdin.read()
    try:
        result = subprocess.run(
            [sys.executable, str(vendor_hook)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=30,
        )
        # Pass through stderr for debuggability; do NOT propagate failure.
        if result.stderr:
            sys.stderr.write(result.stderr)
    except (subprocess.TimeoutExpired, OSError) as e:
        sys.stderr.write(f"agentic-post-tool-global: hook dispatch failed: {e}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
