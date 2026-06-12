"""JSON-safe serialization for query results.

YAML's safe_load happily produces `datetime.date`, `datetime.datetime`, and
similar non-JSON types. The CLI and MCP server both ship results as JSON, so
they share this serializer to coerce values once.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Iterable

from recall.core import QueryResult
from recall.sanitize import provenance_label, sanitize_untrusted


def _to_json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_to_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_json_safe(v) for k, v in value.items()}
    # Anything exotic (custom objects, bytes, sets) → string fallback.
    return str(value)


def _untrusted_field(value: Any) -> Any:
    """JSON-coerce + sanitize a frontmatter-sourced string field.

    `name` and `description` are attacker-influenceable (any ingested doc
    sets them) and ship to model contexts via the CLI and MCP, so they get
    the full untrusted treatment: wrapper-escape neutralization, single
    line, 300-char cap (applied AFTER neutralization)."""
    safe = _to_json_safe(value)
    if isinstance(safe, str):
        return sanitize_untrusted(safe, max_len=300, keep_newlines=False)
    return safe


def serialize_results(results: Iterable[QueryResult]) -> list[dict]:
    """Convert query results into JSON-safe dicts."""
    out: list[dict] = []
    for r in results:
        fm = r.document.frontmatter or {}
        name = fm.get("name") or r.document.title
        out.append(
            {
                "path": str(r.document.path),
                "source": r.document.source,
                "name": _untrusted_field(name),
                "type": _to_json_safe(fm.get("type")),
                "description": _untrusted_field(fm.get("description") or ""),
                "score": round(float(r.score), 6),
                "provenance": provenance_label(fm),
            }
        )
    return out
