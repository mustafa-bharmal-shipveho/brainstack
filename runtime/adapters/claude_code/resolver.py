"""Resolve user-friendly queries to manifest item ids and brain file paths.

Today's CLI requires cryptic ids:
    recall runtime evict c-77ab19d3 --intent

Real day-to-day phrasing is "remove the postgres locking thing" or "add my
postgres lesson." This module turns those queries into concrete ids/paths.

Two helpers, both pure (no I/O on disk for resolve_item; resolve_brain_path
walks the brain directory but doesn't write):

  resolve_item(query, manifest)
      -> ResolutionResult(matched_id, candidates)
      Match priority:
        1. exact id
        2. id prefix (c-...)
        3. source_path basename (case-insensitive)
        4. source_path substring (case-insensitive)
      Stops at the first level that has matches; returns ALL matches at
      that level so callers can show candidates when N > 1.

  resolve_brain_path(query, brain_root)
      -> ResolutionResult(matched_path, candidates)
      Match priority:
        1. exact path that exists
        2. unique file with matching basename anywhere under brain_root
        3. unique file with matching substring in basename
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from runtime.core.manifest import InjectionItemSnapshot, Manifest


@dataclass
class ResolutionResult:
    """Outcome of a resolve_* call.

    - `match` is set when exactly one candidate was found.
    - `candidates` is the full list at the matched priority level (so
      callers can print "did you mean ..." when N > 1 or N == 0).
    - `level` is a human label for the match level used.
    """
    match: str | None
    candidates: list[str]
    level: str


def resolve_item(query: str, manifest: Manifest) -> ResolutionResult:
    """Resolve a query to an item id from the current manifest."""
    q = query.strip()
    if not q:
        return ResolutionResult(None, [], "empty")

    items = list(manifest.items)
    ids = {it.id for it in items}

    # 1. exact id
    if q in ids:
        return ResolutionResult(q, [q], "exact-id")

    # 2. id prefix (e.g., "c-77ab" matches "c-77ab19d3...")
    prefix_matches = sorted(it.id for it in items if it.id.startswith(q))
    if len(prefix_matches) == 1:
        return ResolutionResult(prefix_matches[0], prefix_matches, "id-prefix")
    if len(prefix_matches) > 1:
        return ResolutionResult(None, prefix_matches, "id-prefix")

    q_lower = q.lower()

    # 3. source_path basename match
    base_matches = sorted(
        it.id for it in items
        if Path(it.source_path).name.lower() == q_lower
        or Path(it.source_path).stem.lower() == q_lower
    )
    if len(base_matches) == 1:
        return ResolutionResult(base_matches[0], base_matches, "basename")
    if len(base_matches) > 1:
        return ResolutionResult(None, base_matches, "basename")

    # 4. source_path substring match (case-insensitive)
    substr_matches = sorted(
        it.id for it in items if q_lower in it.source_path.lower()
    )
    if len(substr_matches) == 1:
        return ResolutionResult(substr_matches[0], substr_matches, "substring")
    if len(substr_matches) > 1:
        return ResolutionResult(None, substr_matches, "substring")

    return ResolutionResult(None, [], "no-match")


def resolve_brain_path(query: str, brain_root: Path) -> ResolutionResult:
    """Resolve a query to a file path in the user's brainstack memory.

    Returns the resolved path as a string. Walks brain_root recursively;
    skips hidden dirs and __pycache__.
    """
    q = query.strip()
    if not q:
        return ResolutionResult(None, [], "empty")

    # 1. exact path that exists
    p = Path(q).expanduser()
    if p.exists() and p.is_file():
        return ResolutionResult(str(p), [str(p)], "exact-path")
    # Also try query relative to brain_root
    rel = brain_root / q
    if rel.exists() and rel.is_file():
        return ResolutionResult(str(rel), [str(rel)], "exact-path")

    if not brain_root.exists():
        return ResolutionResult(None, [], "no-brain-root")

    q_lower = q.lower()
    candidates: list[str] = []
    for f in _walk_files(brain_root):
        name = f.name.lower()
        stem = f.stem.lower()
        if name == q_lower or stem == q_lower:
            candidates.append(str(f))
    if len(candidates) == 1:
        return ResolutionResult(candidates[0], candidates, "basename")
    if len(candidates) > 1:
        return ResolutionResult(None, sorted(candidates), "basename")

    # substring on basename
    for f in _walk_files(brain_root):
        if q_lower in f.name.lower():
            candidates.append(str(f))
    candidates = sorted(set(candidates))
    if len(candidates) == 1:
        return ResolutionResult(candidates[0], candidates, "substring")
    if len(candidates) > 1:
        return ResolutionResult(None, candidates, "substring")

    return ResolutionResult(None, [], "no-match")


def _walk_files(root: Path):
    """Yield non-hidden files under root, skipping __pycache__ and similar."""
    skip_names = {"__pycache__", ".git", "node_modules", ".pytest_cache"}
    for entry in root.rglob("*"):
        # Skip hidden dirs / files
        if any(part.startswith(".") for part in entry.relative_to(root).parts):
            continue
        if any(part in skip_names for part in entry.parts):
            continue
        if entry.is_file():
            yield entry


__all__ = ["ResolutionResult", "resolve_brain_path", "resolve_item"]
