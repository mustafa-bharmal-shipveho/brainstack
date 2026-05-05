#!/usr/bin/env python3
"""Misc-dirs adapter for brainstack.

Imports AI tool memory dirs that don't fit any structured-format adapter —
global prompt history, plan files, task files, session metadata, custom
skills, AND each Claude project's curated memory dir — into
`<brain>/imports/<tool>/<dir>/`.

**Mirror, don't swap.** This adapter never modifies the source. Claude and
Codex keep writing to their original folders; we pull from those folders
into the brain on a schedule.

Approach: mirror-copy with mtime-based incremental sync. Each source is a
flat dir; each file is copied verbatim (not transformed beyond redaction)
to a per-tool namespace under the brain. Re-runs are O(N) stat-only and a
no-op if no files changed since last run.

Sources (default; override with --source repeats):
    Claude Code (global):
        ~/.claude/history.jsonl       → ~/.agent/imports/claude/history.jsonl
        ~/.claude/plans/              → ~/.agent/imports/claude/plans/
        ~/.claude/tasks/              → ~/.agent/imports/claude/tasks/
        ~/.claude/sessions/           → ~/.agent/imports/claude/sessions/
        ~/.claude/teams/              → ~/.agent/imports/claude/teams/
        ~/.claude/agents/             → ~/.agent/imports/claude/agents/
        ~/.claude/skills/             → ~/.agent/imports/claude/skills/
        ~/.claude/CLAUDE.md           → ~/.agent/imports/claude/CLAUDE.md
    Claude Code (per-project memory dirs — auto-discovered):
        ~/.claude/projects/<slug>/memory/  →  ~/.agent/imports/claude/projects/<slug>/memory/
        Includes the existing symlinked dir (resolves to ~/.agent/memory which
        is also a project source — safe because rsync skips identical content).
    Cursor:
        ~/.cursor/skills-cursor/      → ~/.agent/imports/cursor/skills-cursor/
        ~/.cursor/ai-tracking/        → ~/.agent/imports/cursor/ai-tracking/

Excluded by default (privacy / volume):
    ~/.claude/paste-cache/      (clipboard pastes — secret risk)
    ~/.claude/file-history/     (file backup snapshots — redundant with git)
    ~/.claude/telemetry/        (not memory)
    ~/.claude/projects/<slug>/*.jsonl (handled by claude_session_adapter)

Redaction: every text file (.md, .json, .jsonl, .txt) passes through
`redact_jsonl.redact_string()` before writing to the brain. Binary files
copy unchanged.

Idempotency: a sidecar at `<brain>/imports/.imported_misc.jsonl` records
(source_path, mtime, size) per file. Files unchanged since the recorded
mtime are skipped.

CLI
---
    claude_misc_adapter.py [--brain ~/.agent] [--dry-run] [--source PATH]...
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Iterator, Optional

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "memory"))

from _atomic import atomic_write_text, atomic_write_bytes  # noqa: E402

try:
    from redact import BUILTIN_PATTERNS  # noqa: E402
    from redact_jsonl import redact_string  # noqa: E402
    _REDACT_AVAILABLE = True
except ImportError:
    BUILTIN_PATTERNS = []  # type: ignore
    redact_string = None  # type: ignore
    _REDACT_AVAILABLE = False


_REDACTABLE_SUFFIXES = frozenset({".md", ".txt", ".json", ".jsonl", ".yml", ".yaml", ".log"})
_SIDECAR_REL = Path("imports") / ".imported_misc.jsonl"


# (source_path, dst_subpath_under_imports). Source path may be a file or dir.
#
# REMOVED (verified secret risk — TruffleHog flagged real creds in these on
# the 2026-05-04 initial sync; they slipped past line-pattern redaction):
#   ~/.claude/history.jsonl   — 5 verified credentials in 1.2 MB of prompts
#   ~/.cursor/ai-tracking     — 928 KB SQLite blob with 77 high-entropy hits
# If you need either, build a content-aware adapter that runs TruffleHog
# on each line/row and only emits clean rows. Don't bulk-import them.
_STATIC_SOURCES: list[tuple[str, str]] = [
    ("~/.claude/plans",             "claude/plans"),
    ("~/.claude/tasks",             "claude/tasks"),
    ("~/.claude/sessions",          "claude/sessions"),
    ("~/.claude/teams",             "claude/teams"),
    ("~/.claude/agents",            "claude/agents"),
    ("~/.claude/skills",            "claude/skills"),
    ("~/.claude/CLAUDE.md",         "claude/CLAUDE.md"),
    ("~/.cursor/skills-cursor",     "cursor/skills-cursor"),
]


def _discover_project_memory_dirs() -> list[tuple[str, str]]:
    """Find every ~/.claude/projects/<slug>/memory/ that has content.

    Returns (source, dst_subpath) tuples. Symlinked memory dirs (e.g. the
    one already pointing at ~/.agent/memory) are skipped — no need to
    mirror the brain into itself.
    """
    sources: list[tuple[str, str]] = []
    projects_root = Path.home() / ".claude" / "projects"
    if not projects_root.is_dir():
        return sources
    try:
        entries = sorted(projects_root.iterdir())
    except OSError:
        return sources
    for proj in entries:
        mem = proj / "memory"
        if mem.is_symlink():
            continue
        if not mem.is_dir():
            continue
        # Skip empty dirs to keep the imports tree clean
        try:
            has_content = any(mem.rglob("*"))
        except OSError:
            has_content = False
        if not has_content:
            continue
        slug = proj.name
        sources.append((str(mem), f"claude/projects/{slug}/memory"))
    return sources


_EXTRA_SOURCES_REL = Path("imports") / "extra_sources.txt"


def _read_extra_sources(brain_root: Path) -> list[tuple[str, str]]:
    """User-defined extra sources from <brain>/imports/extra_sources.txt.

    One entry per line: ``SRC=DST_SUB``. Blank lines and ``#`` comments are
    ignored. ``SRC`` may use ``~`` for $HOME. ``DST_SUB`` is the relative
    path under ``<brain>/imports/`` where the source is mirrored.

    Example::

        # Personal knowledge base — refreshed daily by hand
        ~/Documents/Product & Tech Knowledge Base=kb/product-tech

    Why a config file (not just CLI flags): the LaunchAgent invokes this
    adapter with no arguments. A config file lets users add sources without
    editing the LaunchAgent or the adapter source. Mirrors the same pattern
    as ``<brain>/banner/wrapped_tools``.
    """
    config = brain_root / _EXTRA_SOURCES_REL
    if not config.is_file():
        return []
    out: list[tuple[str, str]] = []
    try:
        text = config.read_text()
    except OSError:
        return []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            print(f"WARN: extra_sources.txt line ignored (no '='): {line!r}", file=sys.stderr)
            continue
        src, dst = line.split("=", 1)
        src = src.strip()
        # Don't lstrip("/") — that would silently turn "/etc/secrets" into
        # "etc/secrets" (bypassing the absolute-path safety check below).
        dst = dst.strip()
        if not src or not dst:
            print(f"WARN: extra_sources.txt line ignored (empty SRC or DST): {line!r}", file=sys.stderr)
            continue
        # Reject path-traversal in DST_SUB. The adapter joins this directly
        # under <brain>/imports/, so a value like "../../outside" would
        # silently write outside the brain on every hourly sync. The header
        # promises DST is *relative under imports*; enforce that.
        if not _is_safe_dst_sub(dst):
            print(
                f"WARN: extra_sources.txt line ignored (unsafe DST — must be a "
                f"relative path under imports/, no '..' or absolute components): {line!r}",
                file=sys.stderr,
            )
            continue
        out.append((src, dst))
    return out


def _is_safe_dst_sub(dst: str) -> bool:
    """True iff DST_SUB is a safe relative path under <brain>/imports/.

    Rejects: empty strings, absolute paths, any component equal to ``..``,
    any component containing a NUL byte. Accepts both Unix-style ``/`` and
    OS-native separators.
    """
    if not dst or "\x00" in dst:
        return False
    p = Path(dst)
    if p.is_absolute():
        return False
    return all(part not in ("", "..") for part in p.parts)


def _build_default_sources(brain_root: Optional[Path] = None) -> list[tuple[str, str]]:
    """Static sources + auto-discovered Claude project memory dirs + user extras."""
    sources = list(_STATIC_SOURCES) + _discover_project_memory_dirs()
    if brain_root is not None:
        sources.extend(_read_extra_sources(brain_root))
    return sources


# Back-compat alias used by older callers
_DEFAULT_SOURCES = _build_default_sources()


def _read_sidecar(path: Path) -> dict[str, dict]:
    """Map of source_path -> {mtime, size, sha256}."""
    seen: dict[str, dict] = {}
    if not path.is_file():
        return seen
    try:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            sp = row.get("source_path")
            if isinstance(sp, str):
                # Last write wins
                seen[sp] = row
    except OSError:
        pass
    return seen


def _append_sidecar(path: Path, entries: list[dict]) -> None:
    if not entries:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text() if path.is_file() else ""
    new = existing + "".join(json.dumps(e) + "\n" for e in entries)
    atomic_write_text(path, new)


def _walk_files(root: Path) -> Iterator[Path]:
    """Yield every regular file under root (depth-first), skipping symlinks."""
    if root.is_file() and not root.is_symlink():
        yield root
        return
    if not root.is_dir() or root.is_symlink():
        return
    try:
        for p in sorted(root.rglob("*")):
            if p.is_symlink():
                continue
            if p.is_file():
                yield p
    except OSError:
        return


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _redact_if_text(path: Path, raw: bytes) -> tuple[bytes, int]:
    """Decode + redact text-like content. Binary returns (raw, 0)."""
    if not _REDACT_AVAILABLE:
        return raw, 0
    if path.suffix.lower() not in _REDACTABLE_SUFFIXES:
        return raw, 0
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw, 0
    new_text, hits = redact_string(text, BUILTIN_PATTERNS)  # type: ignore
    return new_text.encode("utf-8"), len(hits)


def _copy_one(src_file: Path, dst_file: Path) -> tuple[bool, int]:
    """Copy one file with redaction. Returns (changed, redaction_hits)."""
    try:
        raw = src_file.read_bytes()
    except OSError:
        return False, 0
    new_raw, hits = _redact_if_text(src_file, raw)
    dst_file.parent.mkdir(parents=True, exist_ok=True)
    if dst_file.is_file():
        try:
            existing = dst_file.read_bytes()
            if existing == new_raw:
                return False, hits
        except OSError:
            pass
    atomic_write_bytes(dst_file, new_raw)
    return True, hits


def _process_source(
    src_root: Path,
    dst_subpath: str,
    brain_imports: Path,
    sidecar: dict[str, dict],
    dry_run: bool,
) -> tuple[int, int, int, list[dict]]:
    """Sync one source path. Returns (n_files, n_changed, n_redactions, sidecar_updates)."""
    n_files = 0
    n_changed = 0
    n_red = 0
    updates: list[dict] = []
    src_root = src_root.expanduser()
    if not src_root.exists():
        return 0, 0, 0, []

    dst_root = brain_imports / dst_subpath

    for src_file in _walk_files(src_root):
        n_files += 1
        try:
            stat = src_file.stat()
        except OSError:
            continue
        key = str(src_file)
        prev = sidecar.get(key)
        if prev and prev.get("mtime") == stat.st_mtime and prev.get("size") == stat.st_size:
            continue

        # Compute relative path under src_root (or treat single file as bare).
        if src_root.is_file():
            rel = Path(src_root.name)
            dst_file = dst_root  # dst_subpath already includes filename for single-file sources
        else:
            try:
                rel = src_file.relative_to(src_root)
            except ValueError:
                continue
            dst_file = dst_root / rel

        if dry_run:
            n_changed += 1
            # Best-effort redact estimate
            try:
                raw = src_file.read_bytes()
                _, hits = _redact_if_text(src_file, raw)
                n_red += hits
            except OSError:
                pass
            continue

        changed, hits = _copy_one(src_file, dst_file)
        if changed:
            n_changed += 1
        n_red += hits
        updates.append({
            "source_path": key,
            "dst_path": str(dst_file),
            "mtime": stat.st_mtime,
            "size": stat.st_size,
            "sha256": _file_hash(src_file),
            "imported_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "redaction_hits": hits,
        })
    return n_files, n_changed, n_red, updates


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="claude_misc_adapter", description=__doc__.split("\n")[0])
    p.add_argument("--brain", default=str(Path.home() / ".agent"))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--source", action="append", default=[],
                   help="Override sources. Format: SRC=DST_SUB. Can repeat.")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    brain_root = Path(args.brain).expanduser()
    brain_imports = brain_root / "imports"
    sidecar_path = brain_root / _SIDECAR_REL
    sidecar = _read_sidecar(sidecar_path)

    # Build source list
    sources: list[tuple[Path, str]] = []
    if args.source:
        for entry in args.source:
            if "=" not in entry:
                print(f"ERROR: --source requires SRC=DST_SUB; got {entry!r}", file=sys.stderr)
                return 2
            s, d = entry.split("=", 1)
            sources.append((Path(s).expanduser(), d))
    else:
        # Re-discover at runtime so newly-created project memory dirs are picked up
        sources = [(Path(s).expanduser(), d) for s, d in _build_default_sources(brain_root)]

    total_files = 0
    total_changed = 0
    total_redactions = 0
    all_updates: list[dict] = []

    for src, dst_sub in sources:
        if args.verbose:
            print(f"  source: {src} -> imports/{dst_sub}")
        if not src.exists():
            if args.verbose:
                print(f"    (missing — skipped)")
            continue
        nf, nc, nr, upd = _process_source(src, dst_sub, brain_imports, sidecar, args.dry_run)
        if args.verbose:
            print(f"    {nf} files, {nc} changed, {nr} redactions")
        total_files += nf
        total_changed += nc
        total_redactions += nr
        all_updates.extend(upd)

    print(f"\nClaude misc adapter — {'DRY-RUN' if args.dry_run else 'COMPLETE'}")
    print(f"  brain:           {brain_root}")
    print(f"  sources scanned: {len(sources)}")
    print(f"  files seen:      {total_files}")
    print(f"  files changed:   {total_changed}")
    print(f"  redaction hits:  {total_redactions}")

    if not args.dry_run:
        _append_sidecar(sidecar_path, all_updates)
        if all_updates:
            print(f"  sidecar updated: {sidecar_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
