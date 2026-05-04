#!/usr/bin/env python3
"""Claude Code statusline script.

Claude Code calls the configured `statusLine.command` and displays its
stdout in the persistent footer of the chat UI — visible AS SOON AS the
session opens, before the user types anything. That's the surface the
user actually sees on launch (Mustafa 2026-05-04: "can this happen when
the user doesnt write anything and as soon as claude starts").

Output: a single line of text. Empty output suppresses the statusline.

Format on a brain with pending items:
    📥 20 pending — recall pending --review

Empty (no pending, no drift, sync ok):
    (no output → statusline shows whatever default Claude Code uses)

This reads <brain>/PENDING_REVIEW.md if present (cheap — file is
regenerated on every dream/sync tick). If the brain isn't found, exits
silently — Claude Code's statusline must NEVER block session display.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _brain_root() -> Path:
    """Resolve brain root from __file__ first, env-var fallback only.
    Same env-poisoning posture as the SessionStart hook would have had —
    the statusline runs in Claude's environment which a project-level
    .envrc can poison."""
    try:
        here = Path(__file__).resolve()
        # <brain>/tools/statusline.py → <brain>
        candidate = here.parent.parent
        if (candidate / "memory").is_dir() and (candidate / "tools").is_dir():
            return candidate
    except Exception:
        pass
    return Path(os.environ.get("BRAIN_ROOT", str(Path.home() / ".agent")))


def _format_line() -> str:
    """Build the one-line statusline. Empty string means 'don't show'."""
    try:
        pending = _brain_root() / "PENDING_REVIEW.md"
        if not pending.is_file():
            return ""
        text = pending.read_text()
        # All-clear one-liner → empty statusline (no noise on healthy days)
        first_line = text.strip().splitlines()[0] if text.strip() else ""
        if "all clear" in first_line.lower() and len(text.strip().splitlines()) <= 2:
            return ""
        # Find the headline like "**N candidates pending**"
        # Show "📥 N pending: recall pending --review"
        # NOTE: NO em-dash. The user dislikes em-dashes in any output
        # (feedback_no_emdashes memory). Plus em-dashes render poorly in
        # some Claude Code statusline width / encoding combinations
        # ("—n recall ... --rlview" garble seen 2026-05-04). Plain ASCII
        # is safest in the footer.
        import re
        m = re.search(r"\*\*(\d+)\s+candidates\s+pending\*\*", text)
        if m:
            n = int(m.group(1))
            if n > 0:
                return f"brainstack: {n} pending - recall pending --review"
        # Fallback: file exists, has content, but no parseable headline
        return "brainstack: pending - recall pending --review"
    except Exception:
        return ""


def main() -> int:
    try:
        line = _format_line()
        if line:
            sys.stdout.write(line)
            # NOTE: no trailing newline — statusline is single-line
        return 0
    except Exception:
        # Critical: never break Claude Code's UI
        return 0


if __name__ == "__main__":
    sys.exit(main())
