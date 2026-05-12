"""Source-agnostic timestamp normalization for the consolidation framework.

Producers emit `source_ts` in whatever format is natural for them
(some emit float-strings of seconds-since-epoch; others emit ISO-8601
strings; future producers may emit numeric milliseconds, etc.). The
consolidator needs a single comparable scalar for supersession ordering
with NO branching on `source`. This module is the seam.

Cascade (first match wins):
  1. Parse as float; accept if in `[EPOCH_MIN, EPOCH_MAX]` (1970-01-01 to
     2100-01-01). Out-of-range floats are presumed wrong-unit (microseconds
     since epoch, nanoseconds, etc.) and fall through.
  2. Parse as ISO-8601 via `datetime.fromisoformat`.
  3. Fall back to the producer-stamped `ts` (`fallback_iso`) and parse as
     ISO.
  4. If every step fails, raise `SourceTsRangeError`. Consolidator logs and
     skips the event.

EPOCH_MIN includes year 1970 deliberately so historical test fixtures and
backfilled archives stay parseable. EPOCH_MAX is 2100 — far enough that
"someone scheduled a meeting for 2087" still works, close enough that a
microsecond-since-epoch float (e.g. 1.7e15) cannot masquerade as a valid
seconds-since-epoch value.

No source-specific code paths. Adding a new producer with a new timestamp
format means widening the cascade here, not branching on `source`.
"""
from __future__ import annotations

import datetime
from typing import Tuple


_EPOCH_MIN = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc).timestamp()
_EPOCH_MAX = datetime.datetime(2100, 1, 1, tzinfo=datetime.timezone.utc).timestamp()


class SourceTsRangeError(ValueError):
    """Raised when no parsing strategy yields a usable comparable timestamp."""


def _try_float(value: str) -> "float | None":
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if _EPOCH_MIN <= f <= _EPOCH_MAX:
        return f
    return None


def _try_iso(value: str) -> "float | None":
    if not isinstance(value, str):
        return None
    # `datetime.fromisoformat` in 3.9 doesn't accept trailing "Z"; normalize.
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(candidate)
    except ValueError:
        return None
    # Make naive datetimes UTC for consistent epoch math.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    f = dt.timestamp()
    if _EPOCH_MIN <= f <= _EPOCH_MAX:
        return f
    return None


def normalize_source_ts(value: "str | float | int | None",
                        fallback_iso: "str | None" = None) -> Tuple[float, str]:
    """Return `(epoch_seconds, source_label)` for the first strategy that succeeds.

    `value` is the producer's `source_ts` (typically a string but tolerated as
    float/int). `fallback_iso` is the producer's `ts` field (kernel-stamped
    ISO timestamp) which is consulted when the producer's `source_ts` parse
    fails.

    Raises `SourceTsRangeError` if neither `value` nor `fallback_iso` yields
    a usable timestamp inside the sane bounds.
    """
    if value is not None:
        # Tolerate numeric values directly.
        if isinstance(value, (int, float)):
            f = float(value)
            if _EPOCH_MIN <= f <= _EPOCH_MAX:
                return f, "float-epoch-s"
            # Out-of-range numeric: try interpreting as ms / us / ns by
            # scaling down, but only if the scaled value is in-range. This
            # is a single, source-agnostic rescue path.
            for scale, label in ((1e3, "float-epoch-ms"),
                                 (1e6, "float-epoch-us"),
                                 (1e9, "float-epoch-ns")):
                scaled = f / scale
                if _EPOCH_MIN <= scaled <= _EPOCH_MAX:
                    return scaled, label
        else:
            s = str(value)
            f = _try_float(s)
            if f is not None:
                return f, "float-epoch-s"
            f = _try_iso(s)
            if f is not None:
                return f, "iso"

    if fallback_iso is not None:
        f = _try_iso(str(fallback_iso))
        if f is not None:
            return f, "kernel-ts"

    raise SourceTsRangeError(
        f"could not normalize source_ts={value!r} (fallback_iso={fallback_iso!r}); "
        f"acceptable range is epoch seconds in [{_EPOCH_MIN}, {_EPOCH_MAX}]"
    )
