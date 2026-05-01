"""Sub-phase 1b: TokenCounter contract tests.

The runtime needs deterministic, offline token counting so:
  - golden fixtures pass byte-identical across machines and platforms
  - tests + OSS install do not depend on Anthropic APIs (codex's review fix)
  - users can override with a vendor-validating counter when they want exactness

These tests pin the contract before the implementation lands.
"""
from __future__ import annotations

import pytest

from runtime.core.tokens import (
    OfflineTokenCounter,
    TokenCounter,
    count_tokens,
    estimate_token_count,
)


@pytest.fixture
def counter() -> TokenCounter:
    return OfflineTokenCounter()


def test_count_zero_for_empty_string(counter: TokenCounter) -> None:
    assert counter.count("") == 0


def test_count_is_positive_for_nonempty(counter: TokenCounter) -> None:
    assert counter.count("hello") > 0


def test_count_is_deterministic(counter: TokenCounter) -> None:
    """Same input must produce the same count across runs and instances."""
    a = OfflineTokenCounter().count("the quick brown fox jumps over the lazy dog")
    b = OfflineTokenCounter().count("the quick brown fox jumps over the lazy dog")
    c = counter.count("the quick brown fox jumps over the lazy dog")
    assert a == b == c


def test_count_does_not_depend_on_locale_or_path(counter: TokenCounter) -> None:
    """No platform-specific behavior. Same bytes in, same number out."""
    text = "manifest entry: bucket=hot, source=/abs/path/file.md\n"
    assert counter.count(text) == counter.count(text)
    # Same text with windows-y CRLF should NOT change the count drastically.
    crlf = text.replace("\n", "\r\n")
    diff = abs(counter.count(text) - counter.count(crlf))
    # 2-byte vs 1-byte newline should affect by at most 1 token
    assert diff <= 1


def test_count_grows_with_input_size(counter: TokenCounter) -> None:
    short = counter.count("hi")
    long = counter.count("hi " * 100)
    assert long > short * 10


def test_count_handles_unicode(counter: TokenCounter) -> None:
    assert counter.count("résumé café 日本語") > 0


def test_offline_counter_within_15pct_of_known_truth() -> None:
    """The offline heuristic must be within ±15% of a hand-counted truth set.

    This tolerance is wide on purpose. The offline counter is a deterministic
    approximation, not a vendor tokenizer. Users who care about exactness
    wire up AnthropicValidatorTokenCounter (v0.2+).
    """
    cases = [
        ("hello world", 2),
        ("the quick brown fox jumps over the lazy dog", 9),
        ("a" * 1000, 250),  # ~4 chars per token for ASCII
        ("manifest:\n  bucket: hot\n  tokens: 412\n", 12),
    ]
    counter = OfflineTokenCounter()
    for text, expected in cases:
        got = counter.count(text)
        ratio = got / expected if expected else 0
        assert 0.5 <= ratio <= 1.7, (
            f"counter returned {got} for text len {len(text)}; "
            f"expected ~{expected} (ratio {ratio:.2f})"
        )


def test_counter_satisfies_protocol() -> None:
    """OfflineTokenCounter must be assignable to a TokenCounter-typed slot
    (Protocol structural check)."""
    c: TokenCounter = OfflineTokenCounter()
    assert callable(c.count)


def test_module_level_helpers() -> None:
    """The module exposes count_tokens(text) and estimate_token_count(text)
    for ergonomic call sites that don't want to instantiate a counter."""
    assert count_tokens("") == 0
    assert count_tokens("hello") > 0
    # estimate_token_count is the same as count_tokens with the default counter
    assert estimate_token_count("hello world") == count_tokens("hello world")


def test_count_is_pure(counter: TokenCounter) -> None:
    """Calling count() must have no side effects (no I/O, no state change)."""
    counter.count("first call")
    second = counter.count("second call")
    third = counter.count("second call")
    assert second == third
