#!/usr/bin/env python3
"""Discovery manifest for AI-tool memory locations on the local machine.

Walks every known source path and reports:
    - whether it exists
    - file count + total size
    - which adapter handles it (if any)
    - whether it's already covered by the brainstack sync

Useful as a pre-flight audit ("did I miss anything?") and as a post-sync
sanity check ("did everything land?"). Read-only — never writes to disk.

Output: human-readable table by default; JSON with --json.

CLI
---
    discover_all_sources.py [--json] [--brain ~/.agent]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))


# (label, source_path_or_glob, adapter, dst_under_brain, tier, notes)
_KNOWN_SOURCES = [
    # tier 1: covered by structured brainstack adapters
    ("Claude project memory dirs",  "~/.claude/projects/*/memory",
        "claude-code-flat",         "memory/{semantic,personal}",
        1, "Symlinked into brain by --migrate"),
    ("Cursor plans",                "~/.cursor/plans/*.plan.md",
        "cursor-plans",             "memory/imports/cursor",
        1, "Auto-migrate LaunchAgent"),
    ("Codex CLI sessions",          "~/.codex/sessions",
        "codex-cli",                "memory/episodic/codex",
        1, "Auto-migrate LaunchAgent"),
    ("Codex CLI history",           "~/.codex/history.jsonl",
        "codex-cli",                "memory/episodic/codex",
        1, "Auto-migrate LaunchAgent"),
    # tier 2: covered by claude_session_adapter
    ("Claude session transcripts",  "~/.claude/projects/*/*.jsonl",
        "claude-sessions",          "memory/episodic/claude-sessions",
        2, "claude_session_adapter.py"),
    # tier 3: covered by claude_misc_adapter
    ("Claude prompt history",       "~/.claude/history.jsonl",
        "claude-misc",              "imports/claude/history.jsonl",
        3, "claude_misc_adapter.py"),
    ("Claude plans",                "~/.claude/plans",
        "claude-misc",              "imports/claude/plans",
        3, "claude_misc_adapter.py"),
    ("Claude tasks",                "~/.claude/tasks",
        "claude-misc",              "imports/claude/tasks",
        3, "claude_misc_adapter.py"),
    ("Claude sessions metadata",    "~/.claude/sessions",
        "claude-misc",              "imports/claude/sessions",
        3, "claude_misc_adapter.py"),
    ("Claude teams",                "~/.claude/teams",
        "claude-misc",              "imports/claude/teams",
        3, "claude_misc_adapter.py"),
    ("Claude agents",               "~/.claude/agents",
        "claude-misc",              "imports/claude/agents",
        3, "claude_misc_adapter.py"),
    ("Claude skills",               "~/.claude/skills",
        "claude-misc",              "imports/claude/skills",
        3, "claude_misc_adapter.py"),
    ("Claude global CLAUDE.md",     "~/.claude/CLAUDE.md",
        "claude-misc",              "imports/claude/CLAUDE.md",
        3, "claude_misc_adapter.py"),
    ("Cursor custom skills",        "~/.cursor/skills-cursor",
        "claude-misc",              "imports/cursor/skills-cursor",
        3, "claude_misc_adapter.py"),
    ("Cursor AI tracking",          "~/.cursor/ai-tracking",
        "claude-misc",              "imports/cursor/ai-tracking",
        3, "claude_misc_adapter.py"),
    # tier 4: known but skipped
    ("Claude paste-cache",          "~/.claude/paste-cache",
        "(skipped)",                "—",
        4, "Privacy: clipboard paste contents"),
    ("Claude file-history",         "~/.claude/file-history",
        "(skipped)",                "—",
        4, "Redundant with git history"),
    ("Claude telemetry",            "~/.claude/telemetry",
        "(skipped)",                "—",
        4, "Telemetry, not memory"),
    ("Claude Desktop conversations","~/Library/Application Support/Claude",
        "(skipped)",                "—",
        4, "Consumer app, sensitive + 9.5 GB"),
    ("Warp Stable terminal AI",     "~/Library/Application Support/dev.warp.Warp-Stable",
        "(skipped)",                "—",
        4, "Different tool, no adapter built"),
]


def _measure(path_str: str) -> tuple[bool, int, int, bool]:
    """Returns (exists, file_count, total_bytes, is_glob_with_matches)."""
    p = Path(os.path.expanduser(path_str))
    is_glob = "*" in path_str
    if is_glob:
        # Resolve glob
        # split by * to find the un-globbed prefix
        parent = Path(os.path.expanduser(path_str.split("*")[0])).parent
        if not parent.exists():
            return False, 0, 0, True
        try:
            pattern = path_str.replace(os.path.expanduser("~"), str(Path.home()))
            from glob import glob
            matches = [Path(m) for m in glob(os.path.expanduser(path_str), recursive=False)]
        except Exception:
            matches = []
        if not matches:
            return False, 0, 0, True
        nfiles = 0
        size = 0
        for m in matches:
            if m.is_file():
                nfiles += 1
                try:
                    size += m.stat().st_size
                except OSError:
                    pass
            elif m.is_dir():
                for f in m.rglob("*"):
                    if f.is_file() and not f.is_symlink():
                        nfiles += 1
                        try:
                            size += f.stat().st_size
                        except OSError:
                            pass
        return True, nfiles, size, True

    if not p.exists():
        return False, 0, 0, False
    if p.is_file():
        try:
            return True, 1, p.stat().st_size, False
        except OSError:
            return True, 1, 0, False
    nfiles = 0
    size = 0
    for f in p.rglob("*"):
        if f.is_file() and not f.is_symlink():
            nfiles += 1
            try:
                size += f.stat().st_size
            except OSError:
                pass
    return True, nfiles, size, False


def _humanize(b: int) -> str:
    if b < 1024:
        return f"{b}B"
    for unit in ("KB", "MB", "GB", "TB"):
        b /= 1024  # type: ignore
        if abs(b) < 1024:
            return f"{b:.1f}{unit}"
    return f"{b:.1f}PB"


def _check_brainstack_state(brain_root: Path) -> dict:
    state = {}
    state["episodic_main"] = (brain_root / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl").is_file()
    state["episodic_codex"] = (brain_root / "memory" / "episodic" / "codex" / "AGENT_LEARNINGS.jsonl").is_file()
    state["episodic_claude_sessions"] = (brain_root / "memory" / "episodic" / "claude-sessions" / "AGENT_LEARNINGS.jsonl").is_file()
    state["imports_dir"] = (brain_root / "imports").is_dir()
    state["auto_migrate_config"] = (brain_root / "auto-migrate.json").is_file()
    state["auto_migrate_plist"] = (Path.home() / "Library" / "LaunchAgents" /
                                   "com.brainstack.auto-migrate.plist").is_file()
    state["sync_plist"] = (Path.home() / "Library" / "LaunchAgents" /
                           "com.user.agent-sync.plist").is_file()
    return state


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="discover_all_sources", description=__doc__.split("\n")[0])
    p.add_argument("--json", action="store_true", help="Emit JSON instead of a table")
    p.add_argument("--brain", default=str(Path.home() / ".agent"))
    args = p.parse_args(argv)

    brain_root = Path(args.brain).expanduser()
    rows = []
    totals = {1: 0, 2: 0, 3: 0, 4: 0}
    for label, src, adapter, dst, tier, notes in _KNOWN_SOURCES:
        exists, nfiles, nbytes, _is_glob = _measure(src)
        rows.append({
            "label": label,
            "source": src,
            "exists": exists,
            "files": nfiles,
            "size": nbytes,
            "size_human": _humanize(nbytes),
            "adapter": adapter,
            "dst": dst,
            "tier": tier,
            "notes": notes,
        })
        if exists:
            totals[tier] += nbytes

    state = _check_brainstack_state(brain_root)

    if args.json:
        print(json.dumps({"sources": rows, "brain_state": state}, indent=2))
        return 0

    print("=" * 110)
    print(f"AI memory source manifest — brain: {brain_root}")
    print("=" * 110)
    for tier in (1, 2, 3, 4):
        tier_rows = [r for r in rows if r["tier"] == tier]
        if not tier_rows:
            continue
        labels = {1: "TIER 1 — Structured adapter (auto-migrate hourly)",
                  2: "TIER 2 — Claude session transcripts (custom adapter)",
                  3: "TIER 3 — Misc dirs (rsync-style adapter)",
                  4: "TIER 4 — Skipped (privacy / volume / not memory)"}
        print(f"\n{labels[tier]}  [total: {_humanize(totals[tier])}]")
        print("-" * 110)
        for r in tier_rows:
            mark = "✅" if r["exists"] else "  "
            print(f"  {mark} {r['label']:<32} {r['size_human']:>8}  {r['files']:>5} files   "
                  f"{r['adapter']:<18}  {r['notes']}")
            if r["exists"]:
                print(f"        source: {r['source']}")
                print(f"        dst:    brain/{r['dst']}")

    print()
    print("=" * 110)
    print("Brainstack state:")
    print("-" * 110)
    print(f"  episodic main JSONL:                 {'✅' if state['episodic_main'] else '❌'}  "
          f"~/.agent/memory/episodic/AGENT_LEARNINGS.jsonl")
    print(f"  episodic codex namespace:            {'✅' if state['episodic_codex'] else '❌'}  "
          f"~/.agent/memory/episodic/codex/")
    print(f"  episodic claude-sessions namespace:  {'✅' if state['episodic_claude_sessions'] else '❌'}  "
          f"~/.agent/memory/episodic/claude-sessions/")
    print(f"  imports dir:                         {'✅' if state['imports_dir'] else '❌'}  "
          f"~/.agent/imports/")
    print(f"  auto-migrate config:                 {'✅' if state['auto_migrate_config'] else '❌'}  "
          f"~/.agent/auto-migrate.json")
    print(f"  auto-migrate LaunchAgent:            {'✅' if state['auto_migrate_plist'] else '❌'}  "
          f"~/Library/LaunchAgents/com.brainstack.auto-migrate.plist")
    print(f"  hourly sync LaunchAgent:             {'✅' if state['sync_plist'] else '❌'}  "
          f"~/Library/LaunchAgents/com.user.agent-sync.plist")
    return 0


if __name__ == "__main__":
    sys.exit(main())
