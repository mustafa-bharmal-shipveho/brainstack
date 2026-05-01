"""Persistent "forget this" — archive a lesson out of semantic/lessons.

Counterpart to `recall remember`. Resolves a query (lesson name / substring)
to a concrete file under ~/.agent/memory/semantic/lessons/, then moves it to
~/.agent/memory/semantic/archived/<timestamp>-<name>.md so it's recoverable
if you change your mind.

Uses the same resolver as the runtime CLI (basename → substring) so the UX
is consistent.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path

from runtime.adapters.claude_code.resolver import resolve_brain_path

from recall.remember import DEFAULT_BRAIN_ROOT, LESSONS_SUBDIR

ARCHIVED_SUBDIR = "memory/semantic/archived"


@dataclass
class ForgetResult:
    """Outcome of an attempted forget. Either archived_path is set, or
    candidates contains the alternatives that need disambiguating."""
    archived_path: Path | None
    candidates: list[Path]


def archive_lesson(query: str, brain_root: Path = DEFAULT_BRAIN_ROOT) -> ForgetResult:
    """Find a lesson matching `query` and move it to the archived dir.

    Returns a ForgetResult: archived_path on unique match, candidates list
    on multi-match, both empty if nothing matched.
    """
    lessons_dir = brain_root / LESSONS_SUBDIR
    if not lessons_dir.exists():
        return ForgetResult(None, [])

    result = resolve_brain_path(query, lessons_dir)
    if result.match is None:
        return ForgetResult(None, [Path(c) for c in result.candidates])

    src = Path(result.match)
    archive_dir = brain_root / ARCHIVED_SUBDIR
    archive_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")
    dest = archive_dir / f"{ts}-{src.name}"
    src.rename(dest)
    return ForgetResult(archived_path=dest, candidates=[dest])


__all__ = ["ARCHIVED_SUBDIR", "ForgetResult", "archive_lesson"]
