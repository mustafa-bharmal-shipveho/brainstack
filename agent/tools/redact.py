#!/usr/bin/env python3
"""Pre-commit secret scanner.

Scans a directory tree for known public-token patterns (AWS keys, GitHub
tokens, JWTs, generic high-entropy secrets in `key: value` shape).

Behavior:
  - Walks the target directory recursively.
  - Skips binary files (any file containing a NUL byte in the first 8KB).
  - Respects per-line allowlist marker `# redact-allow: <reason>` —
    if the marker appears on the same line OR on the line immediately
    before the match, the match is suppressed.
  - Exits 0 if no matches.
  - Exits 1 with output `<file>:<line>:<pattern_name>: <matched-text>` for
    each hit, on stderr-style stdout (we use stdout for ease of piping).

Intended use as a pre-commit hook:

    #!/usr/bin/env bash
    python3 ~/.agent/tools/redact.py ~/.agent/ || exit 1

Note: this is the PUBLIC layer. Org-specific patterns (Veho-shaped tokens,
internal hostnames) belong in `~/.agent/redact_private.py` which lives in
the user's private brain repo, not in this public framework.

Usage:
    python3 redact.py <target-dir>
"""
import os
import re
import sys
from pathlib import Path
from typing import Iterable

# ----- Patterns -----
# Each entry: (name, compiled-regex). Names appear in the output, so use
# something searchable and unambiguous.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github_pat", re.compile(r"ghp_[A-Za-z0-9]{36}")),
    ("github_oauth", re.compile(r"gho_[A-Za-z0-9]{36}")),
    ("github_server", re.compile(r"ghs_[A-Za-z0-9]{36}")),
    ("github_user_app", re.compile(r"ghu_[A-Za-z0-9]{36}")),
    ("github_refresh", re.compile(r"ghr_[A-Za-z0-9]{36}")),
    ("jwt_three_part", re.compile(
        r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"
    )),
    # Generic high-entropy in key: value or key=value form.
    # 30+ chars to avoid false positives on git hashes (40 hex chars).
    ("generic_secret_assignment", re.compile(
        r"""(?ix)
        \b(api[_\-]?key|secret(?:[_\-]?key)?|password|passwd|token|auth[_\-]?token|access[_\-]?token)
        \s*[:=]\s*
        ['"]?
        ([A-Za-z0-9_\-]{30,})
        ['"]?
        """,
        re.VERBOSE,
    )),
]


# ----- Allowlist -----
# Per-line marker: any line containing this string suppresses redaction
# for itself AND the next line.
ALLOWLIST_MARKER_RE = re.compile(r"#\s*redact-allow\b", re.IGNORECASE)


def is_binary(path: Path) -> bool:
    """Return True if file appears binary (NUL byte in first 8KB)."""
    try:
        with path.open("rb") as f:
            chunk = f.read(8192)
        return b"\x00" in chunk
    except OSError:
        # Unreadable — treat as binary so we skip it silently
        return True


def iter_files(root: Path) -> Iterable[Path]:
    """Yield text files under root, skipping .git/ and binary files."""
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        # Skip git internals
        if ".git" in p.parts:
            continue
        # Skip __pycache__ and similar
        if "__pycache__" in p.parts:
            continue
        if is_binary(p):
            continue
        yield p


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    """Return list of (line_number, pattern_name, matched_text) hits."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []

    lines = text.splitlines()
    # Build allowlist line-set: lines containing the marker, plus the line after
    allow_lines: set[int] = set()
    for i, line in enumerate(lines, start=1):
        if ALLOWLIST_MARKER_RE.search(line):
            allow_lines.add(i)
            allow_lines.add(i + 1)

    hits: list[tuple[int, str, str]] = []
    for i, line in enumerate(lines, start=1):
        if i in allow_lines:
            continue
        for name, pat in PATTERNS:
            m = pat.search(line)
            if m:
                # Take the first matched group if any, else the whole match
                matched = m.group(0)
                # For generic_secret_assignment, the value capture is group 2
                if name == "generic_secret_assignment" and m.lastindex and m.lastindex >= 2:
                    matched = m.group(2)
                hits.append((i, name, matched))
                break  # Don't report multiple patterns for the same line
    return hits


def main():
    if len(sys.argv) != 2:
        print("usage: redact.py <target-dir>", file=sys.stderr)
        return 2

    root = Path(sys.argv[1]).resolve()
    if not root.exists():
        print(f"redact: target not found: {root}", file=sys.stderr)
        return 2

    total_hits = 0
    for f in iter_files(root):
        hits = scan_file(f)
        for line_no, pattern_name, matched in hits:
            # Truncate matched value for safer output (don't print full secret)
            display = matched[:8] + "..." if len(matched) > 12 else matched
            print(f"{f}:{line_no}:{pattern_name}: {display}")
            total_hits += 1

    if total_hits:
        print(f"\nredact: {total_hits} potential secret(s) found. Commit blocked.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
