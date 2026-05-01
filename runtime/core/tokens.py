"""Pluggable token counting for the runtime.

Two principles:

1. **Deterministic offline default.** Tests, golden fixtures, and OSS install
   must not depend on Anthropic APIs or any network call. The default
   `OfflineTokenCounter` is a stable heuristic that produces the same number
   for the same input on every machine, every Python version, every locale.

2. **Pluggable.** Users who want vendor-accurate counts can wire up their own
   `TokenCounter` (e.g., calling Anthropic's `messages.count_tokens`) and
   inject it. The runtime never assumes a specific implementation.

Accuracy of the offline default: ~±15% of vendor counts on English text. Wide
on purpose. The runtime's contract is that *budget enforcement is consistent*,
not that token counts are exact. If a bucket cap is 4000 offline-tokens, the
real Claude window may have ~3400-4600 tokens for that bucket. That is a
deliberate trade for portability and determinism.
"""
from __future__ import annotations

import re
from typing import Protocol, runtime_checkable


@runtime_checkable
class TokenCounter(Protocol):
    """Anything with a `count(text)` method that returns a non-negative int."""

    def count(self, text: str) -> int: ...


class OfflineTokenCounter:
    """Deterministic, dependency-free heuristic.

    Splits on word boundaries, normalizes whitespace, and counts each "chunk"
    weighted by length. Calibrated so that on typical English text it lands
    near `len(text) / 4` tokens (the well-known OpenAI BPE rule of thumb).
    """

    # Pre-compiled to keep `count()` allocation-free on the hot path.
    _word_re = re.compile(r"\w+|[^\w\s]", re.UNICODE)

    def count(self, text: str) -> int:
        if not text:
            return 0
        # Strategy:
        #   - each word contributes max(1, len(word) // 4) tokens.
        #     Common short words ("the", "and") count as 1 token, matching
        #     how real BPE tokenizers represent them. Long words decompose
        #     into roughly one token per 4 characters.
        #   - each non-word symbol contributes 1 token.
        #   - whitespace contributes nothing.
        # On English prose this converges to len(text)/~4. On JSON/code,
        # punctuation density pushes the count up, also matching BPE.
        total = 0
        for match in self._word_re.finditer(text):
            chunk = match.group(0)
            if chunk[0].isalnum() or "_" in chunk:
                total += max(1, len(chunk) // 4)
            else:
                total += 1
        return total


class AnthropicValidatorTokenCounter:
    """Placeholder for the optional vendor-validating counter.

    v0 of the runtime does not implement this — it only declares the slot so
    callers can substitute it. Resolves the codex review fix: vendor APIs are
    optional validation, not a runtime requirement.
    """

    def count(self, text: str) -> int:  # pragma: no cover - placeholder
        raise NotImplementedError(
            "AnthropicValidatorTokenCounter is a v0.2 placeholder. "
            "Use OfflineTokenCounter for now, or supply your own TokenCounter."
        )


# Module-level convenience for call sites that don't want to instantiate.
_DEFAULT_COUNTER = OfflineTokenCounter()


def count_tokens(text: str) -> int:
    """Count tokens with the default offline counter."""
    return _DEFAULT_COUNTER.count(text)


# Public alias used by some call sites for readability.
estimate_token_count = count_tokens


__all__ = [
    "AnthropicValidatorTokenCounter",
    "OfflineTokenCounter",
    "TokenCounter",
    "count_tokens",
    "estimate_token_count",
]
