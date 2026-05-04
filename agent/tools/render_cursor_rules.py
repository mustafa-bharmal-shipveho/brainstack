#!/usr/bin/env python3
"""Pushes the pending-review summary into ~/.cursor/.cursorrules between
sentinels so Cursor surfaces it on every chat session.

Sentinel-bracketed update preserves any user-authored rules above and below
the brainstack section. Idempotent — re-running with the same content is
a no-op.

CLI
---
    render_cursor_rules.py [--brain DIR] [--cursor-dir DIR]

  --brain DIR        Brain root (default: $BRAIN_ROOT or ~/.agent). Reads
                     PENDING_REVIEW.md from here.
  --cursor-dir DIR   Cursor config dir (default: ~/.cursor). If missing
                     (Cursor not installed), exits 0 silently.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "memory"))

from _atomic import atomic_write_text  # noqa: E402


_SENTINEL_START = "<!-- brainstack-pending-start -->"
_SENTINEL_END = "<!-- brainstack-pending-end -->"


def _build_block(content: str) -> str:
    """Wrap content in sentinels with a header."""
    return (
        f"{_SENTINEL_START}\n"
        "## brainstack pending review\n\n"
        f"{content.rstrip()}\n\n"
        "_To triage: open Claude Code and run `/dream`, or "
        "`recall pending --review` from a shell._\n"
        f"{_SENTINEL_END}"
    )


def update_cursorrules(content: str, cursorrules_path: Path) -> bool:
    """Update the sentinel-bracketed section of `cursorrules_path`.
    Returns True if the file was changed."""
    cursorrules_path = Path(cursorrules_path)
    new_block = _build_block(content)

    if not cursorrules_path.is_file():
        # Create from scratch
        cursorrules_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(cursorrules_path, new_block + "\n")
        return True

    existing = cursorrules_path.read_text()

    if _SENTINEL_START in existing and _SENTINEL_END in existing:
        # Replace the bracketed section in-place
        start_idx = existing.index(_SENTINEL_START)
        end_idx = existing.index(_SENTINEL_END) + len(_SENTINEL_END)
        new_text = existing[:start_idx] + new_block + existing[end_idx:]
    else:
        # Append the block to the existing content
        sep = "" if existing.endswith("\n") else "\n"
        sep += "\n" if not existing.endswith("\n\n") else ""
        new_text = existing + sep + new_block + "\n"

    if new_text == existing:
        return False
    atomic_write_text(cursorrules_path, new_text)
    return True


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="render_cursor_rules")
    p.add_argument(
        "--brain",
        default=os.environ.get("BRAIN_ROOT", str(Path.home() / ".agent")),
        help="Brain root (default: $BRAIN_ROOT or ~/.agent)",
    )
    p.add_argument(
        "--cursor-dir",
        default=str(Path.home() / ".cursor"),
        help="Cursor config dir (default: ~/.cursor)",
    )
    args = p.parse_args(argv)

    brain_root = Path(args.brain).expanduser()
    cursor_dir = Path(args.cursor_dir).expanduser()

    if not cursor_dir.is_dir():
        # Cursor not installed → silent no-op (do NOT create the dir)
        print(f"render_cursor_rules: {cursor_dir} not found; skipping")
        return 0

    pending_file = brain_root / "PENDING_REVIEW.md"
    if pending_file.is_file():
        try:
            content = pending_file.read_text()
        except OSError as e:
            sys.stderr.write(f"render_cursor_rules: read failed {pending_file}: {e}\n")
            return 1
    else:
        content = "✅ all clear (no pending review items)"

    cursorrules_path = cursor_dir / ".cursorrules"
    changed = update_cursorrules(content, cursorrules_path)
    if changed:
        print(f"render_cursor_rules: updated {cursorrules_path}")
    else:
        print(f"render_cursor_rules: {cursorrules_path} already up-to-date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
