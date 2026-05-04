#!/usr/bin/env python3
"""Claude Code SessionStart hook. Reads <brain>/PENDING_REVIEW.md and
emits a JSON response telling Claude Code to inject the content into
the session context.

Output format (Claude Code SessionStart hook contract):

    {"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": "<the markdown content>"
    }}

Raw stdout (without the JSON envelope) is IGNORED by Claude Code —
that was the original bug Mustafa caught when the banner didn't appear
on a fresh session despite the hook being correctly registered.

Four behaviors that MUST hold (tests pin them):
  1. Silent on missing file (brain not yet generated).
  2. Silent on the "all clear" one-liner (no noise on healthy sessions).
  3. NEVER raises. Any exception → return 0. SessionStart hooks block
     the session until they finish; an uncaught crash here would brick
     Claude Code on every launch.
  4. Brain root resolves from __file__, NOT from $HOME / $BRAIN_ROOT.
     The hook is registered with an absolute path in settings.json
     (env-poisoning protection from PostToolUse — same threat model).
     Trusting $HOME here would let a project-level .envrc redirect us
     to attacker-controlled content that gets injected verbatim into
     the additionalContext field (Codex 2026-05-04 P1 — prompt
     injection).

The hook lives at <brain>/harness/hooks/session_start.py, so
`__file__.parent.parent.parent` is always the trusted brain root.
Tests still set BRAIN_ROOT for fixture isolation; we honor it ONLY when
__file__ resolution fails (defensive fallback).

Wired up via:
  ~/.claude/settings.json — see adapters/claude-code/settings.snippet.json
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _resolve_brain_root() -> Path:
    """Trusted resolution: walk up from this file's path. Falls back to
    $BRAIN_ROOT only if the structural walk doesn't yield a plausible
    brain (file relocated, symlinked weirdly). Never falls back to $HOME
    — that's the env-poisoning vector Codex flagged."""
    try:
        here = Path(__file__).resolve()
        # <brain>/harness/hooks/session_start.py  →  <brain>
        candidate = here.parent.parent.parent
        # Sanity check: a real brain has memory/ and tools/ subdirs
        if (candidate / "memory").is_dir() and (candidate / "tools").is_dir():
            return candidate
    except Exception:
        pass
    # Fallback only — explicit env override (used in tests)
    env = os.environ.get("BRAIN_ROOT")
    if env:
        return Path(env)
    # Last-resort default; will likely yield "missing" but never raise
    return Path.home() / ".agent"


def _emit_additional_context(content: str) -> None:
    """Emit the Claude Code SessionStart hook JSON envelope.

    Claude Code reads stdout and expects either a JSON object with
    hookSpecificOutput.additionalContext set, OR raw text it ignores.
    Raw text => ignored => banner doesn't appear (the bug Mustafa hit).
    """
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": content,
        }
    }
    sys.stdout.write(json.dumps(payload))
    sys.stdout.write("\n")


def main() -> int:
    try:
        brain_root = _resolve_brain_root()
        pending = brain_root / "PENDING_REVIEW.md"
        if not pending.is_file():
            return 0  # nothing to surface
        try:
            content = pending.read_text()
        except OSError:
            return 0  # unreadable → don't block session start
        # Suppress the "all clear" one-liner. Heuristic: a single line
        # starting with U+2705 ("✅") or "all clear" is treated as
        # the empty-state signal.
        stripped = content.strip()
        if not stripped:
            return 0
        first_line = stripped.splitlines()[0]
        if "all clear" in first_line.lower() and len(stripped.splitlines()) <= 2:
            return 0
        # Wrap the markdown in a <system-reminder> block (so Claude treats
        # it as system context) and emit via the SessionStart JSON envelope
        # so Claude Code actually injects it into the session.
        wrapped = f"<system-reminder>\n{content.rstrip()}\n</system-reminder>"
        _emit_additional_context(wrapped)
        return 0
    except Exception:
        # Critical contract: NEVER let an exception propagate. SessionStart
        # hooks are blocking; any crash here delays / breaks session launch.
        return 0


if __name__ == "__main__":
    sys.exit(main())
