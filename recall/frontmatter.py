"""YAML frontmatter parsing for markdown memory files.

Parses the leading `---`-delimited YAML block of a markdown file. Designed to be
liberal: malformed YAML, missing closers, BOMs, and CRLF line endings all
degrade gracefully (frontmatter becomes empty, body is preserved).

Hardened against pathological inputs:
- Frontmatter blocks larger than `_MAX_FRONTMATTER_BYTES` are skipped (treated
  as no frontmatter) to bound parse time and prevent YAML billion-laughs
  expansion from running unchecked.
- `yaml.safe_load` is used so `!!python/object` constructors are rejected.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

# Frontmatter is supposed to hold a curated header — a few tens of fields at
# most. 256 KiB is far more than any realistic memory file needs but
# small enough that pathological inputs (anchor expansion, MB-scale single
# values) can't burn meaningful CPU.
_MAX_FRONTMATTER_BYTES = 256 * 1024


@dataclass(frozen=True)
class ParsedFile:
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""


_BOM = "﻿"


def parse_file_text(text: str) -> ParsedFile:
    """Parse a markdown text into ParsedFile(frontmatter, body).

    Returns an empty frontmatter dict (and body == original text) if the input
    has no leading frontmatter, or if the YAML between the markers fails to
    parse. Never raises — invalid input degrades gracefully.
    """
    if not text:
        return ParsedFile(frontmatter={}, body="")

    # Strip BOM if present
    if text.startswith(_BOM):
        text = text[len(_BOM) :]

    # Normalize line endings to LF for consistent splitting
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")

    if not normalized.startswith("---\n") and normalized != "---" and not normalized.startswith("---"):
        return ParsedFile(frontmatter={}, body=text)

    if not normalized.startswith("---\n"):
        # Could be "---" with no newline (file ends right there) or "---\r\n" already handled.
        # Anything that doesn't start with "---\n" after normalization isn't a real frontmatter.
        return ParsedFile(frontmatter={}, body=text)

    # Find the closing "---" on its own line, after the opening
    lines = normalized.split("\n")
    closer_idx = -1
    for i in range(1, len(lines)):
        if lines[i] == "---":
            closer_idx = i
            break

    if closer_idx == -1:
        # No closer — treat the whole file as body
        return ParsedFile(frontmatter={}, body=text)

    yaml_text = "\n".join(lines[1:closer_idx])
    body_lines = lines[closer_idx + 1 :]
    body = "\n".join(body_lines)

    # Refuse oversized frontmatter blocks before handing to the YAML parser.
    # This bounds parse time and stops anchor-bomb YAML from expanding.
    if len(yaml_text.encode("utf-8", errors="replace")) > _MAX_FRONTMATTER_BYTES:
        return ParsedFile(frontmatter={}, body=body)

    # If original used \r\n, preserve no specific style — body is canonicalized to \n
    try:
        loaded = yaml.safe_load(yaml_text) if yaml_text.strip() else {}
    except yaml.YAMLError:
        # Malformed YAML — preserve the original text as body so nothing is lost
        return ParsedFile(frontmatter={}, body=text)

    if not isinstance(loaded, dict):
        # Frontmatter must be a mapping; lists or scalars are malformed
        return ParsedFile(frontmatter={}, body=body)

    return ParsedFile(frontmatter=loaded, body=body)


def parse_path(path: Path) -> ParsedFile:
    """Read a file from disk and parse it. Tolerates non-UTF8 / binary content."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # Likely binary or wrong encoding. Try latin-1 as a permissive fallback.
        try:
            text = path.read_text(encoding="latin-1")
        except OSError:
            return ParsedFile(frontmatter={}, body="")
    return parse_file_text(text)


def normalize_unicode(text: str) -> str:
    """NFC-normalize for consistent comparison across HFS+ NFD vs other-FS NFC."""
    return unicodedata.normalize("NFC", text)


# ---------------------------------------------------------------------------
# Temporal validity (Phase 2)
#
# `temporal_meta` is the single normalization point for the temporal fields a
# memory doc may carry: status / valid_from / valid_until / superseded_by /
# supersedes. It is a *view* over the frontmatter dict — ParsedFile is
# unchanged, and docs written before these fields existed still parse to
# sensible defaults (status="current", unbounded validity). It never raises.
# ---------------------------------------------------------------------------

# Canonical temporal status domain. Explicit `status:` values are aliased into
# it; anything unrecognized becomes "unknown" (never an error).
_STATUS_ALIASES = {
    "current": "current",
    "accepted": "current",  # lessons.jsonl-style status on markdown
    "staged": "staged",
    "provisional": "staged",
    "needs_review": "staged",
    "superseded": "superseded",
    "stale": "stale",
}


@dataclass(frozen=True)
class TemporalMeta:
    """Normalized temporal view over a doc's frontmatter."""

    status: str  # current | staged | superseded | stale | unknown
    valid_from: Optional[str] = None  # ISO-8601 or None
    valid_until: Optional[str] = None
    superseded_by: Optional[str] = None
    supersedes: Optional[str] = None


def parse_iso_datetime(value: Any) -> Optional[str]:
    """Coerce a YAML-loaded value to an ISO-8601 string; garbage → None."""
    import datetime as _dt

    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value.isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            # fromisoformat parses both dates and datetimes; PyYAML emits "Z"
            # only in strings, which older parsers reject, so normalize it.
            _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return text
    return None


def _str_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def temporal_meta(frontmatter: dict[str, Any] | None) -> TemporalMeta:
    """Normalize temporal fields from a frontmatter dict. Never raises.

    Defaults for missing keys (the backwards-compat contract):
    - no `status`, no `needs_review` → status="current"
    - `needs_review` truthy, no `status` → status="staged"
    - `stance: superseded` or `type: claim-stale` → status="superseded"
    - no `valid_from` → None (unbounded past, not a parse error)
    """
    if not isinstance(frontmatter, dict):
        frontmatter = {}

    raw_status = frontmatter.get("status")
    if isinstance(raw_status, str) and raw_status:
        status = _STATUS_ALIASES.get(raw_status, "unknown")
    elif raw_status is not None:
        status = "unknown"  # present but not a usable string
    elif frontmatter.get("stance") == "superseded" or frontmatter.get("type") == "claim-stale":
        status = "superseded"
    elif frontmatter.get("needs_review"):
        status = "staged"
    else:
        status = "current"

    return TemporalMeta(
        status=status,
        valid_from=parse_iso_datetime(frontmatter.get("valid_from")),
        valid_until=parse_iso_datetime(frontmatter.get("valid_until")),
        superseded_by=_str_or_none(frontmatter.get("superseded_by")),
        supersedes=_str_or_none(frontmatter.get("supersedes")),
    )
