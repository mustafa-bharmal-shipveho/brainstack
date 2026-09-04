"""Lenient value coercions + window formatting for the report builders.

`recall stats` and `recall stats --utilization` both read the same JSONL
telemetry, written by a hook that must never crash a prompt. A malformed
field is therefore normal input, not an error: every read goes through one
of these, which substitute a default instead of raising. Both report
modules had their own byte-identical copies, which is exactly how two
readers of one log start disagreeing about what a bad value means.

Stdlib-only and importing nothing from `recall`, so the lowest-level
readers can use it freely.
"""

from __future__ import annotations

import datetime


def as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def as_list(value: object) -> list:
    return value if isinstance(value, list) else []


def as_mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def format_window(since_ts_ms: "int | None") -> str:
    """The ` (since YYYY-MM-DD)` / ` (all time)` suffix on a report header."""
    if since_ts_ms is None:
        return " (all time)"
    start = datetime.datetime.fromtimestamp(
        since_ts_ms / 1000, tz=datetime.timezone.utc
    ).date().isoformat()
    return f" (since {start})"


__all__ = ["as_float", "as_int", "as_list", "as_mapping", "format_window"]
