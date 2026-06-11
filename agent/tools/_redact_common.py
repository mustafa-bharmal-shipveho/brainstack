"""Shared write-path redaction for brain adapters.

Single chokepoint for "redact this text before it lands in the brain":
builtin single-line patterns + multi-line patterns (PEM blocks) + the
user's private patterns from ``<brain_root>/redact-private.txt`` + the
Shannon-entropy sweep for opaque 32+ char tokens that match no named
vendor pattern.

Extracted from ``claude_session_digest_adapter._redact_text`` so the
codex / cursor / session / misc adapters all share one implementation
instead of each wiring its own (incomplete) pattern set.

Failure policy: fail OPEN. A malformed line in ``redact-private.txt``
gets a one-time stderr WARN (via ``redact.load_private_patterns``) and
the builtin coverage still applies. A user typo must never make the
adapters silently stop importing, and must never disable the builtins.
Redaction itself never raises into the write path.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Path setup so we can import sibling modules without packaging.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from redact import (  # noqa: E402
    BUILTIN_PATTERNS,
    ENTROPY_DEFAULT_THRESHOLD,
    MULTILINE_PATTERNS,
    load_private_patterns,
)
from redact_jsonl import redact_string  # noqa: E402

# Patterns cached per brain_root so a long backfill doesn't re-read
# redact-private.txt once per item.
_PATTERN_CACHE: dict[str, list] = {}


def patterns_for(brain_root: Path | str) -> list:
    """Builtin + multiline + private patterns for ``brain_root``, cached.

    The private-pattern load fails open: ``load_private_patterns`` warns
    on stderr and skips malformed lines itself; any unexpected exception
    around the load is also caught and warned, keeping builtin coverage.
    """
    key = str(brain_root)
    patterns = _PATTERN_CACHE.get(key)
    if patterns is None:
        patterns = list(BUILTIN_PATTERNS) + list(MULTILINE_PATTERNS)
        try:
            patterns += list(load_private_patterns(Path(brain_root)))
        except Exception as e:  # fail open, never block the write path
            sys.stderr.write(
                f"WARN: redact_for_write: load_private_patterns failed "
                f"({type(e).__name__}: {e}); using builtin patterns only\n"
            )
        _PATTERN_CACHE[key] = patterns
    return patterns


def redact_for_write(text: str, brain_root: Path | str) -> str:
    """Redact ``text`` before it is written into the brain at ``brain_root``.

    Applies builtin + multiline + private patterns plus the entropy sweep.
    Surgical: only matched tokens are replaced; surrounding text survives.
    Idempotent: ``[REDACTED:...]`` markers never re-match. Never raises;
    on an unexpected redaction failure the original text is returned
    (fail open, same policy as the per-adapter wrappers it replaces).
    """
    if not text:
        return text
    try:
        redacted, _hits = redact_string(
            text, patterns_for(brain_root),
            entropy_threshold=ENTROPY_DEFAULT_THRESHOLD,
        )
        return redacted
    except Exception as e:  # pragma: no cover - defensive fail-open
        sys.stderr.write(
            f"WARN: redact_for_write: redaction failed "
            f"({type(e).__name__}: {e}); writing text unredacted\n"
        )
        return text


__all__ = ["patterns_for", "redact_for_write"]
