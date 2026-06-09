"""Render a structured digest dict to the two surfaces recall consumes:

  A. An episodic JSONL line (vector-index recall, short payload).
  B. A markdown file with YAML front-matter (browse, long-form recall,
     git-syncable).

Contract pinned by `tests/test_digest_render.py`. Both writes are atomic-
enough: the episodic append uses an open-append-flush; the markdown write
goes through atomic_write_text (sibling .tmp + os.replace).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import sys
from pathlib import Path

# Reuse the project's atomic write helper.
_THIS = Path(__file__).resolve()
_MEMORY = _THIS.parent.parent / "memory"
if str(_MEMORY) not in sys.path:
    sys.path.insert(0, str(_MEMORY))
from _atomic import atomic_write_text  # type: ignore


# ---------------------------------------------------------------------------
# Episodic line
# ---------------------------------------------------------------------------

def _importance_from_salience(salience: int | float | None) -> int:
    """Clamp salience (1–10) to importance (1–10). LLM might return
    weird values — treat anything out of range as the midpoint."""
    if isinstance(salience, (int, float)):
        v = int(round(float(salience)))
        if 1 <= v <= 10:
            return v
    return 5


def render_episodic(digest: dict, meta: dict) -> dict:
    """Build the episode dict for the JSONL line. Field shape mirrors
    `claude_session_adapter._extract_episodes` so downstream consumers
    (recall index, cluster.py) don't have to special-case digests."""
    source = meta.get("source", "claude")
    salience = digest.get("salience")
    importance = _importance_from_salience(salience)
    pain = max(1, min(10, 11 - importance))
    # Conditions = domain_tags so cluster.py groups digests by topic.
    domain_tags = digest.get("domain_tags") or []
    if not isinstance(domain_tags, list):
        domain_tags = []
    conditions = sorted({str(t).strip().lower()
                         for t in domain_tags if str(t).strip()})

    return {
        "timestamp": meta.get("started_at")
                     or datetime.datetime.now(datetime.timezone.utc)
                                          .isoformat(),
        "skill": "session-digest",
        "action": str(digest.get("title") or "")[:200],
        "result": "success",
        "detail": json.dumps({
            "decisions": digest.get("decisions") or [],
            "files_touched": digest.get("files_touched") or [],
            "outcome": digest.get("outcome"),
        })[:4096],
        "pain_score": pain,
        "importance": importance,
        "reflection": str(digest.get("what_was_learned") or ""),
        "confidence": 0.75,
        "source": {
            "adapter": "session-digest",
            "session_id": meta.get("session_id"),
            "project_slug": meta.get("project_slug"),
            "model": meta.get("model"),
            "cwd": meta.get("cwd"),
        },
        "conditions": conditions,
        "origin": f"session.digest.{source}",
        "summary": str(digest.get("what_user_did") or ""),
    }


# ---------------------------------------------------------------------------
# Markdown render
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(s: str, max_len: int = 60) -> str:
    s = s.lower()
    s = _SLUG_RE.sub("-", s).strip("-")
    if not s:
        s = "untitled"
    return s[:max_len].rstrip("-") or "untitled"


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def markdown_path_for(digest: dict, meta: dict, *,
                      base_dir: Path) -> Path:
    """Compute the path for the markdown digest. Format:
    `<YYYY-MM-DD>__<slug>__<sid-suffix>.md`

    The `<sid-suffix>` (last 8 chars of session_id, or a hash if shorter)
    guarantees uniqueness even when two same-day sessions have identical
    title-derived slugs.

    Path traversal defense: the date segment is validated against
    `^\\d{4}-\\d{2}-\\d{2}$`. Malformed/malicious timestamps (e.g.
    containing `/` or `..`) fall through to `0000-00-00`. Slug + suffix
    are already sanitized by their respective helpers."""
    started = meta.get("started_at") or ""
    date = started[:10] if started else ""
    if not _DATE_RE.match(date):
        date = "0000-00-00"
    slug = _slugify(str(digest.get("title") or "untitled"))
    sid = str(meta.get("session_id") or "")
    if len(sid) >= 8:
        suffix = sid[-8:]
    else:
        suffix = hashlib.sha1(sid.encode()).hexdigest()[:8]
    suffix = re.sub(r"[^a-z0-9]+", "", suffix.lower()) or "0"
    name = f"{date}__{slug}__{suffix}.md"
    candidate = Path(base_dir) / name
    # Defense in depth: ensure the resolved path stays under base_dir.
    # Should never trigger after the above sanitization, but a future
    # refactor could regress.
    try:
        resolved = candidate.resolve()
        base_resolved = Path(base_dir).resolve()
        if not str(resolved).startswith(str(base_resolved) + os.sep) \
                and resolved != base_resolved:
            raise ValueError(
                f"digest markdown path escapes base_dir: {candidate}"
            )
    except (OSError, ValueError):
        raise
    return candidate


_YAML_SAFE_BARE_RE = re.compile(r"^[A-Za-z0-9_\-./:+@][A-Za-z0-9_\-./:+@ ]*$")
_YAML_RESERVED = {"true", "false", "yes", "no", "null", "~", ""}


def _yaml_safe(s: str) -> str:
    """Single-line YAML scalar. Emit bare when safe; double-quote
    otherwise. Quoting is conservative — anything with embedded
    newlines, leading/trailing space, YAML reserved words, or special
    characters gets quoted + escaped."""
    if (
        s
        and _YAML_SAFE_BARE_RE.match(s)
        # ": " (colon+space) and a trailing ":" are YAML mapping indicators;
        # " #" starts a comment. A bare scalar containing any of these is
        # invalid YAML even though the chars are individually "safe" (e.g.
        # "19:33:48" is fine bare, but "Scope negotiated: 502" is not).
        and ": " not in s
        and not s.endswith(":")
        and " #" not in s
        and s.strip().lower() not in _YAML_RESERVED
        and not s.startswith("-")
        # A leading YAML indicator char (e.g. "@mention", "`code`") is reserved
        # and breaks a bare scalar even though the rest of the value is safe.
        and s[0] not in "@`!&*?|>%#"
        and not s[0].isdigit()  # don't risk timestamp/number coercion
    ):
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') \
                  .replace("\n", "\\n") + '"'


def _yaml_list(items: list[str]) -> str:
    # Inside a flow list, leave items individually quote-as-needed —
    # commas and brackets in any element will force quoting via the
    # regex above (those chars aren't in the safe-bare set).
    safe = [_yaml_safe(str(i)) for i in items]
    return "[" + ", ".join(safe) + "]"


def render_markdown(digest: dict, meta: dict) -> str:
    """Build the markdown body with YAML front matter."""
    domain_tags = digest.get("domain_tags") or []
    if not isinstance(domain_tags, list):
        domain_tags = []
    decisions = digest.get("decisions") or []
    files_touched = digest.get("files_touched") or []

    front = [
        "---",
        f"session_id: {_yaml_safe(str(meta.get('session_id') or ''))}",
        f"source: {meta.get('source', 'claude')}",
        f"started_at: {meta.get('started_at') or ''}",
        f"ended_at: {meta.get('ended_at') or ''}",
        f"cwd: {_yaml_safe(str(meta.get('cwd') or ''))}",
        f"git_branch: {_yaml_safe(str(meta.get('git_branch') or ''))}",
        f"project_slug: {_yaml_safe(str(meta.get('project_slug') or ''))}",
        f"model: {_yaml_safe(str(meta.get('model') or ''))}",
        f"domain_tags: {_yaml_list(domain_tags)}",
        # _yaml_safe: outcome is free LLM text and often contains a colon
        # (e.g. "Scope negotiated: ..."), which breaks unquoted YAML and made
        # the whole frontmatter unparseable. Quote it.
        f"outcome: {_yaml_safe(str(digest.get('outcome', 'unknown')))}",
        f"salience: {_importance_from_salience(digest.get('salience'))}",
        "---",
        "",
        f"# {digest.get('title') or 'Untitled'}",
        "",
        "## What you did",
        "",
        str(digest.get("what_user_did") or ""),
        "",
        "## What was learned",
        "",
        str(digest.get("what_was_learned") or ""),
        "",
        "## Decisions",
        "",
    ]
    if decisions:
        for d in decisions:
            front.append(f"- {d}")
    else:
        front.append("_(none recorded)_")
    front += ["", "## Files touched", ""]
    if files_touched:
        for f in files_touched:
            front.append(f"- `{f}`")
    else:
        front.append("_(none recorded)_")
    front.append("")
    return "\n".join(front)


# ---------------------------------------------------------------------------
# Dual write
# ---------------------------------------------------------------------------

# Match an optional surrounding quote on the truthy value, so a flag written
# as `needs_review: 'true'` / `"yes"` is preserved across re-render too — must
# stay consistent with recall.core / recall.lint, which both accept the quoted
# form (otherwise _carry_needs_review would silently drop a quoted flag).
_NEEDS_REVIEW_RE = re.compile(r"""needs_review\s*:\s*['"]?(true|yes|1)\b""", re.IGNORECASE)


def _existing_needs_review(path: Path) -> bool:
    """True if `path` already carries a top-level `needs_review` flag.

    Markdown digests are overwritten by deterministic path, so a re-render
    would silently drop a flag added later (e.g. by `recall lint --mark`).
    We detect it here so write_dual can carry it across the rewrite.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    if not text.startswith("---"):
        return False
    for line in text.replace("\r\n", "\n").split("\n")[1:]:
        if line.strip() == "---":
            break
        if _NEEDS_REVIEW_RE.match(line):
            return True
    return False


def _carry_needs_review(md_body: str) -> str:
    """Splice `needs_review: true` into a freshly-rendered digest's
    frontmatter (idempotent). md_body is LF-newlined with a leading
    `---`-delimited block."""
    lines = md_body.split("\n")
    if not lines or lines[0] != "---":
        return md_body
    for i in range(1, len(lines)):
        if lines[i] == "---":
            if any(_NEEDS_REVIEW_RE.match(ln) for ln in lines[1:i]):
                return md_body  # already present
            lines.insert(i, "needs_review: true")
            return "\n".join(lines)
    return md_body


def write_dual(digest: dict, meta: dict, *,
               episodic_path: Path | str,
               markdown_dir: Path | str) -> dict:
    """Write both surfaces. Episodic is append-only; markdown is
    overwrite by deterministic path. Returns paths actually written."""
    episodic_path = Path(episodic_path)
    markdown_dir = Path(markdown_dir)
    episodic_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_dir.mkdir(parents=True, exist_ok=True)

    # Markdown — atomic via temp file + replace
    md_path = markdown_path_for(digest, meta, base_dir=markdown_dir)
    md_body = render_markdown(digest, meta)
    # Preserve a review flag a human/lint added to a prior render of this
    # exact digest, so re-rendering never silently un-flags a stale memory.
    if _existing_needs_review(md_path):
        md_body = _carry_needs_review(md_body)
    atomic_write_text(md_path, md_body)

    # Episodic — append-only newline-delimited JSON
    ep_line = render_episodic(digest, meta)
    with open(episodic_path, "a") as f:
        f.write(json.dumps(ep_line) + "\n")

    return {
        "episodic_path": str(episodic_path),
        "markdown_path": str(md_path),
    }
