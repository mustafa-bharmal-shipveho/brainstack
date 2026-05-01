"""Persistent "remember this" — write a lesson to brainstack's semantic/lessons.

Different from `recall runtime add` which is session-scoped (re-injection on
the next user prompt). `recall remember` writes a markdown file to
~/.agent/memory/semantic/lessons/<slug>.md so brainstack auto-loads it on
EVERY future session forever.

Frontmatter matches the existing convention used by feedback_*.md lessons:

    ---
    name: <slug>
    description: <one-line>
    type: lesson
    source: recall-remember
    created: <ISO8601>
    ---

    <body of the lesson, the user's natural-language advice>

Also touches the directory's mtime so any "did anything change?" watcher
notices.
"""
from __future__ import annotations

import datetime
import re
from pathlib import Path

DEFAULT_BRAIN_ROOT = Path("~/.agent").expanduser()
LESSONS_SUBDIR = "memory/semantic/lessons"


def _slugify(text: str, max_len: int = 60) -> str:
    """Turn natural-language text into a filesystem-safe slug.

    Lowercase, strip non-alphanumeric/space/dash, collapse whitespace + dashes
    into single dashes, truncate to `max_len`.
    """
    s = text.lower().strip()
    s = re.sub(r"[^\w\s-]", " ", s)
    s = re.sub(r"[\s_-]+", "-", s)
    s = s.strip("-")
    if not s:
        return "lesson"
    return s[:max_len].rstrip("-")


def _first_line(text: str, max_len: int = 100) -> str:
    """First non-empty line of `text`, truncated."""
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:max_len]
    return ""


def write_lesson(
    text: str,
    name: str | None = None,
    description: str | None = None,
    brain_root: Path = DEFAULT_BRAIN_ROOT,
    *,
    overwrite: bool = False,
) -> Path:
    """Write a lesson markdown file to <brain_root>/memory/semantic/lessons/<slug>.md.

    Args:
        text: the lesson body. Plain markdown. The user's natural-language advice.
        name: optional explicit slug. Defaults to a slug of the first line.
        description: one-line summary. Defaults to the first line of `text`.
        brain_root: brainstack memory root. Defaults to ~/.agent.
        overwrite: if False and the file exists, raise FileExistsError.

    Returns:
        The full path the lesson was written to.
    """
    if not text.strip():
        raise ValueError("lesson text cannot be empty")

    lessons_dir = brain_root / LESSONS_SUBDIR
    if not lessons_dir.exists():
        raise FileNotFoundError(
            f"brainstack lessons dir not found at {lessons_dir}. "
            f"Run install.sh first or pass --brain-root."
        )

    slug = _slugify(name or _first_line(text))
    target = lessons_dir / f"{slug}.md"
    if target.exists() and not overwrite:
        raise FileExistsError(
            f"lesson '{slug}' already exists at {target}. "
            f"Pick a different --as name, or pass --overwrite."
        )

    desc = description or _first_line(text, max_len=120) or slug
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    body = (
        f"---\n"
        f"name: {slug}\n"
        f"description: {desc}\n"
        f"type: lesson\n"
        f"source: recall-remember\n"
        f"created: {now}\n"
        f"---\n"
        f"\n"
        f"{text.strip()}\n"
    )
    target.write_text(body, encoding="utf-8")
    return target


__all__ = ["DEFAULT_BRAIN_ROOT", "LESSONS_SUBDIR", "write_lesson"]
