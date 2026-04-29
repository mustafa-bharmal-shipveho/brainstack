"""Source plugins: file discovery + frontmatter handling per source mode."""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from recall.config import SourceConfig
from recall.frontmatter import parse_path

_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Document:
    path: str
    source: str
    title: str
    frontmatter: dict
    body: str
    text: str  # the indexed text (name + description*3 + body)


def _matches_any(rel_path: str, patterns: list[str]) -> bool:
    """Match a posix-style relative path against fnmatch patterns.

    Treats `dir/**` as 'anything inside dir/'.
    """
    if not patterns:
        return False
    for pat in patterns:
        if fnmatch.fnmatch(rel_path, pat):
            return True
        # Handle `**` semantics: fnmatch `**` doesn't match across path
        # separators by default. Implement a simple expansion.
        if "**" in pat:
            # Expand `dir/**` → match anything under dir/
            simplified = pat.replace("**", "*")
            if fnmatch.fnmatch(rel_path, simplified):
                return True
            # Also match the dir prefix itself
            if pat.endswith("/**"):
                prefix = pat[:-3]
                if rel_path == prefix or rel_path.startswith(prefix + "/"):
                    return True
    return False


def _walk_md_files(root: Path) -> Iterator[Path]:
    """Walk root, yielding *.md files. Does NOT follow directory symlinks
    (so symlinked directory loops can't blow up the walk). Caller should
    enforce containment for symlinked files separately."""
    if not root.exists():
        return
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for fn in filenames:
            if fn.endswith(".md"):
                yield Path(dirpath) / fn


def _resolves_inside(target: Path, root: Path) -> bool:
    """True if `target` resolves to a path inside `root` (after symlink resolution)."""
    try:
        target_real = target.resolve(strict=False)
        root_real = root.resolve(strict=False)
    except OSError:
        return False
    try:
        target_real.relative_to(root_real)
        return True
    except ValueError:
        return target_real == root_real


def _glob_to_regex(pattern: str) -> "re.Pattern[str]":
    """Convert a glob pattern to a regex.

    Semantics:
        `*`   → matches any chars except `/`
        `**`  → matches any chars including `/`
        `**/` → matches zero or more leading directory components

    `**/*.md` matches `foo.md` AND `a/b/foo.md`. `*.md` matches only top-level
    `foo.md` (not nested).
    """
    out: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern[i : i + 3] == "**/":
            # Zero-or-more leading directory components
            out.append("(?:.*/)?")
            i += 3
        elif pattern[i : i + 2] == "**":
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c in ".+^$()[]{}|\\":
            out.append(re.escape(c))
            i += 1
        else:
            out.append(c)
            i += 1
    return re.compile("\\A" + "".join(out) + "\\Z")


_GLOB_CACHE: dict[str, "re.Pattern[str]"] = {}


def _glob_matches(rel_path: str, pattern: str) -> bool:
    """True if rel_path matches the glob pattern."""
    if not pattern or pattern in ("*", "**", "**/*"):
        return True
    cached = _GLOB_CACHE.get(pattern)
    if cached is None:
        cached = _glob_to_regex(pattern)
        _GLOB_CACHE[pattern] = cached
    return cached.match(rel_path) is not None


def _extract_h1(body: str) -> str | None:
    m = _H1_RE.search(body)
    return m.group(1).strip() if m else None


def _build_indexed_text(name: str, description: str, body: str) -> str:
    # Description gets 3x weight by repetition (cheap, works across rankers)
    weighted_desc = " ".join([description] * 3) if description else ""
    return f"{name} {weighted_desc} {body}".strip()


def discover_documents(source: SourceConfig) -> Iterator[Document]:
    root = Path(source.resolved_path)
    if not root.exists():
        return

    for file_path in _walk_md_files(root):
        try:
            rel = file_path.relative_to(root)
        except ValueError:
            continue
        rel_posix = rel.as_posix()

        # Honor the configured glob pattern. Defaults to `**/*.md`.
        if not _glob_matches(rel_posix, source.glob):
            continue

        if _matches_any(rel_posix, source.exclude):
            continue

        # Symlinked files: only follow if they resolve inside the source root.
        # Prevents an attacker (or accidental symlink) from leaking arbitrary
        # files into search results.
        if file_path.is_symlink() and not _resolves_inside(file_path, root):
            continue

        try:
            parsed = parse_path(file_path)
        except OSError:
            continue

        # Skip empty files
        if not parsed.frontmatter and not parsed.body.strip():
            continue

        # Source mode determines fallback behavior
        fm = parsed.frontmatter or {}
        name = str(fm.get("name") or "") or file_path.stem

        # Title: prefer frontmatter name, then H1, then filename stem
        title = name or _extract_h1(parsed.body) or file_path.stem
        if not fm and source.frontmatter == "optional":
            # Plain markdown — try to use H1 as title
            h1 = _extract_h1(parsed.body)
            if h1:
                title = h1

        description = str(fm.get("description") or "")
        text = _build_indexed_text(name, description, parsed.body)

        yield Document(
            path=str(file_path),
            source=source.name,
            title=title,
            frontmatter=fm,
            body=parsed.body,
            text=text,
        )
