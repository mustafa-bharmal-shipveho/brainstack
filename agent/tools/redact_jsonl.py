#!/usr/bin/env python3
"""In-place JSONL secret scrubber.

The PostToolUse hook captures raw Bash commands, Edit text, and tool output
into the episodic JSONL — *before* redaction runs. By the time sync.sh runs
its scanner, the JSONL has already accumulated secrets that flowed through
tool calls (Authorization headers in curl output, etc.).

This script walks one or more JSONL files and rewrites every string field
recursively, replacing secret-shaped substrings with `[REDACTED:<name>]`.
The whole file is rewritten atomically (temp + fsync + os.replace) so a
SIGKILL cannot leave the file torn.

Usage:
    redact_jsonl.py <file_or_dir> [<file_or_dir> ...]
    redact_jsonl.py --dry-run <file_or_dir>

Exit codes:
    0 — no changes (or dry-run with no would-be changes)
    1 — changes applied (or dry-run with would-be changes); CI-friendly
    2 — fatal error (file unreadable, JSONL malformed)

Designed to run inside `sync.sh` *before* staging. Idempotent — running
twice on the same file is a no-op once it's clean.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# Reuse the pattern set from redact.py — single source of truth.
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from redact import (  # noqa: E402
    BUILTIN_PATTERNS,
    MULTILINE_PATTERNS,
    load_private_patterns,
)


REDACTED = "[REDACTED:{name}]"


def redact_string(s: str, patterns: list) -> tuple[str, list[str]]:
    """Return (redacted_string, list_of_pattern_names_hit)."""
    hits: list[str] = []
    out = s
    # Single-line patterns: replace the matched (sub)group
    for name, pat in patterns:
        def _sub(m, _name=name):
            hits.append(_name)
            # For grouped patterns, replace only the secret-bearing group so
            # we keep the field name visible in the redacted output (helps
            # debugging without leaking the value).
            if _name == "generic_secret_assignment" and m.lastindex and m.lastindex >= 2:
                whole = m.group(0)
                value = m.group(2)
                return whole.replace(value, REDACTED.format(name=_name))
            if _name in ("auth_bearer", "auth_basic") and m.lastindex and m.lastindex >= 1:
                whole = m.group(0)
                value = m.group(1)
                return whole.replace(value, REDACTED.format(name=_name))
            return REDACTED.format(name=_name)

        out = pat.sub(_sub, out)

    # Multi-line patterns (PEM blocks, etc.) — full-replace
    for name, pat in MULTILINE_PATTERNS:
        def _sub_ml(m, _name=name):
            hits.append(_name)
            return REDACTED.format(name=_name)
        out = pat.sub(_sub_ml, out)
    return out, hits


def scrub_value(v: Any, patterns: list, hits_acc: list[str]) -> Any:
    """Recursively scrub all string values in a JSON-shaped object."""
    if isinstance(v, str):
        new, hits = redact_string(v, patterns)
        hits_acc.extend(hits)
        return new
    if isinstance(v, list):
        return [scrub_value(x, patterns, hits_acc) for x in v]
    if isinstance(v, dict):
        return {k: scrub_value(val, patterns, hits_acc) for k, val in v.items()}
    return v


def atomic_write(path: Path, data: str) -> None:
    """Write `data` to `path` atomically (temp + fsync + replace)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        # Best-effort cleanup of the temp file
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def process_jsonl(path: Path, patterns: list, dry_run: bool) -> tuple[int, int]:
    """Process a single JSONL file. Returns (lines_changed, total_hits)."""
    try:
        text = path.read_text()
    except OSError as e:
        sys.stderr.write(f"redact-jsonl: cannot read {path}: {e}\n")
        return 0, 0

    out_lines: list[str] = []
    lines_changed = 0
    total_hits = 0

    for line_no, line in enumerate(text.splitlines(keepends=False), start=1):
        if not line.strip():
            out_lines.append(line)
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            sys.stderr.write(
                f"redact-jsonl: malformed JSON at {path}:{line_no}: {e} (kept verbatim)\n"
            )
            out_lines.append(line)
            continue

        hits: list[str] = []
        scrubbed = scrub_value(obj, patterns, hits)

        if hits:
            lines_changed += 1
            total_hits += len(hits)
            print(
                f"{path}:{line_no}: scrubbed {len(hits)} secret(s) "
                f"({', '.join(sorted(set(hits)))})"
            )
        out_lines.append(json.dumps(scrubbed, ensure_ascii=False, separators=(",", ":")))

    if lines_changed and not dry_run:
        atomic_write(path, "\n".join(out_lines) + ("\n" if text.endswith("\n") else ""))
    return lines_changed, total_hits


def find_jsonls(target: Path) -> list[Path]:
    if target.is_file():
        return [target] if target.suffix == ".jsonl" else []
    return sorted(target.rglob("*.jsonl"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Scrub secrets from JSONL files in place.")
    ap.add_argument("targets", nargs="+", help="JSONL files or directories")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be changed; don't write",
    )
    ap.add_argument(
        "--brain-root",
        default=os.path.expanduser("~/.agent"),
        help="Brain root for loading redact-private.txt (default: ~/.agent)",
    )
    args = ap.parse_args()

    brain_root = Path(args.brain_root)
    extra = load_private_patterns(brain_root) if brain_root.exists() else []
    patterns = BUILTIN_PATTERNS + extra

    jsonls: list[Path] = []
    for t in args.targets:
        p = Path(t)
        if not p.exists():
            sys.stderr.write(f"redact-jsonl: not found: {p}\n")
            return 2
        jsonls.extend(find_jsonls(p))

    if not jsonls:
        return 0

    total_changed = 0
    total_hits = 0
    for jl in jsonls:
        c, h = process_jsonl(jl, patterns, args.dry_run)
        total_changed += c
        total_hits += h

    if total_changed:
        prefix = "would scrub" if args.dry_run else "scrubbed"
        sys.stderr.write(
            f"\nredact-jsonl: {prefix} {total_hits} secret(s) "
            f"across {total_changed} line(s)\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
