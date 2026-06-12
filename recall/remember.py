"""Persistent "remember this" — write a lesson to brainstack's semantic/lessons.

Different from `recall runtime add` which is session-scoped (re-injection on
the next user prompt). `recall remember` writes a markdown file to
~/.agent/memory/semantic/lessons/<slug>.md so brainstack auto-loads it on
EVERY future session forever.

Frontmatter matches the existing convention used by feedback_*.md lessons,
plus the trust-workstream review gate. By default a remembered lesson is
STAGED for human review (any agent, or an injected prompt, can drive
`recall remember`; staging keeps those writes from silently becoming
durable memory):

    ---
    name: <slug>
    description: <one-line>
    type: lesson
    source: recall-remember
    created_by: recall-remember
    provenance: human-cli | agent       (stdin TTY heuristic, self-reported)
    created: <ISO8601>
    needs_review: true                  (absent when reviewed=True)
    review_reason: unreviewed-remember  (absent when reviewed=True)
    ---

    <body of the lesson, the user's natural-language advice>

With `reviewed=True` (the CLI's --reviewed flag, a human decision) the
lesson is durable immediately: `reviewed_by: human-cli` replaces the two
staging keys. The retrieval review policy demotes or excludes staged
lessons until `recall pending --review` accepts them.

Also touches the directory's mtime so any "did anything change?" watcher
notices.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
from pathlib import Path


def _yaml_str(value: str) -> str:
    """Render a string as a YAML-safe scalar for a frontmatter value.

    A JSON-encoded string is a valid YAML double-quoted scalar, so this
    handles embedded colons, quotes, and backslashes that would otherwise
    produce invalid YAML (which parsers then read as empty frontmatter).
    """
    return json.dumps(value, ensure_ascii=False)

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
    reviewed: bool = False,
    created_by: str | None = None,
    session_id: str | None = None,
) -> Path:
    """Write a lesson markdown file to <brain_root>/memory/semantic/lessons/<slug>.md.

    Args:
        text: the lesson body. Plain markdown. The user's natural-language advice.
        name: optional explicit slug. Defaults to a slug of the first line.
        description: one-line summary. Defaults to the first line of `text`.
        brain_root: brainstack memory root. Defaults to ~/.agent.
        overwrite: if False and the file exists, raise FileExistsError.
        reviewed: False (default) stages the lesson for human review
            (needs_review + review_reason frontmatter); True writes a
            durable lesson stamped reviewed_by: human-cli. Only set True
            on an explicit human decision (the CLI's --reviewed flag).
        created_by: writer attribution. Defaults to "recall-remember".
        session_id: originating session. Falls back to $CLAUDE_SESSION_ID
            then $BRAINSTACK_SESSION_ID; omitted when none is available.

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
    sid = (
        session_id
        or os.environ.get("CLAUDE_SESSION_ID")
        or os.environ.get("BRAINSTACK_SESSION_ID")
    )
    # Self-reported provenance, not a signature: a TTY on stdin means a
    # human at the CLI; anything else (hooks, agents, pipes) is "agent".
    try:
        is_tty = sys.stdin.isatty()
    except (AttributeError, ValueError):
        is_tty = False

    # Quote free-text values with json.dumps: a JSON string is a valid YAML
    # double-quoted scalar, so an embedded ": " (e.g. a description like
    # "fix: do X") cannot corrupt the frontmatter into invalid YAML that every
    # parser then reads as empty (silently dropping needs_review/provenance).
    fm_lines = [
        f"name: {_yaml_str(slug)}",
        f"description: {_yaml_str(desc)}",
        "type: lesson",
        "source: recall-remember",
        f"created_by: {created_by or 'recall-remember'}",
        f"provenance: {'human-cli' if is_tty else 'agent'}",
        f"created: {now}",
    ]
    if sid:
        fm_lines.append(f"session_id: {sid}")
    if reviewed:
        fm_lines.append("reviewed_by: human-cli")
    else:
        fm_lines.append("needs_review: true")
        fm_lines.append("review_reason: unreviewed-remember")

    body = "---\n" + "\n".join(fm_lines) + "\n---\n\n" + text.strip() + "\n"
    target.write_text(body, encoding="utf-8")
    return target


__all__ = ["DEFAULT_BRAIN_ROOT", "LESSONS_SUBDIR", "write_lesson"]
