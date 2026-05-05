#!/usr/bin/env python3
"""Pushes the pending-review summary into ~/.codex/AGENTS.md (and an
optional repo-level AGENTS.md) between sentinels so Codex CLI surfaces
it on every chat session.

Codex CLI reads `AGENTS.md` files at session start (per OpenAI's docs:
similar pattern to Claude's CLAUDE.md). It walks CWD up to home looking
for AGENTS.md files and concatenates their content into the model's
system context. ~/.codex/AGENTS.md is the global file applied to every
Codex CLI session regardless of CWD.

Reuses `render_cursor_rules.update_cursorrules` — both tools manage a
sentinel-bracketed section in a markdown file, idempotent, preserves
surrounding user content. The function name is general; only the target
path differs per tool.

CLI
---
    render_codex_agents_md.py [--brain DIR] [--codex-dir DIR]

  --brain DIR       Brain root (default: $BRAIN_ROOT or ~/.agent)
  --codex-dir DIR   Codex config dir (default: ~/.codex). If missing,
                    exits 0 silently (Codex not installed).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from render_cursor_rules import update_cursorrules  # noqa: E402


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="render_codex_agents_md")
    p.add_argument(
        "--brain",
        default=os.environ.get("BRAIN_ROOT", str(Path.home() / ".agent")),
        help="Brain root (default: $BRAIN_ROOT or ~/.agent)",
    )
    p.add_argument(
        "--codex-dir",
        default=str(Path.home() / ".codex"),
        help="Codex CLI config dir (default: ~/.codex)",
    )
    args = p.parse_args(argv)

    brain_root = Path(args.brain).expanduser()
    codex_dir = Path(args.codex_dir).expanduser()

    if not codex_dir.is_dir():
        print(f"render_codex_agents_md: {codex_dir} not found; skipping")
        return 0

    pending_file = brain_root / "PENDING_REVIEW.md"
    if pending_file.is_file():
        try:
            content = pending_file.read_text()
        except OSError as e:
            sys.stderr.write(
                f"render_codex_agents_md: read failed {pending_file}: {e}\n"
            )
            return 1
    else:
        content = "all clear (no pending review items)"

    agents_md = codex_dir / "AGENTS.md"
    changed = update_cursorrules(content, agents_md)
    if changed:
        print(f"render_codex_agents_md: updated {agents_md}")
    else:
        print(f"render_codex_agents_md: {agents_md} already up-to-date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
