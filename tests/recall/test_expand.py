"""Unit tests for query expansion (recall/expand.py).

The expansion module delegates to the brainstack LLM provider registry.
These tests mock the provider so the suite is hermetic (no Claude / Codex
CLI required).
"""
from __future__ import annotations

import json
from typing import Optional
from unittest.mock import patch

import pytest

from recall import expand as expand_module


class _StubProvider:
    """Minimal stand-in for an LLMProvider that returns scripted JSON."""

    def __init__(self, paraphrases: list[str], *, raise_exc: Exception | None = None):
        self._paraphrases = paraphrases
        self._raise_exc = raise_exc
        self.invoke_calls: list[dict] = []

    def invoke(self, system, prompt, *, model=None, json_schema=None, max_budget_usd=0.10, timeout_s=60):
        self.invoke_calls.append({"system": system, "prompt": prompt})
        if self._raise_exc is not None:
            raise self._raise_exc
        # Lightweight result object that has a .text attribute
        class _R:
            def __init__(self, text: str):
                self.text = text
        return _R(json.dumps({"paraphrases": self._paraphrases}))


@pytest.fixture(autouse=True)
def _clear_cache():
    """Reset the LRU cache between tests so each test gets a fresh provider call."""
    expand_module._cached_expand.cache_clear()
    yield
    expand_module._cached_expand.cache_clear()


def _patch_provider(stub: _StubProvider):
    """Helper to patch resolve_provider at the import site used by expand.py."""
    return patch("agent.tools.llm_providers.resolve_provider", return_value=stub)


class TestExpandQuery:
    def test_returns_original_plus_paraphrases(self):
        stub = _StubProvider(["alt one", "alt two", "alt three"])
        with _patch_provider(stub):
            out = expand_module.expand_query("original query", n=3)
        assert out == ["original query", "alt one", "alt two", "alt three"]
        assert len(stub.invoke_calls) == 1

    def test_n_zero_short_circuits_no_llm_call(self):
        stub = _StubProvider([])
        with _patch_provider(stub):
            out = expand_module.expand_query("q", n=0)
        assert out == ["q"]
        assert stub.invoke_calls == [], "no LLM call when n=0"

    def test_caches_repeat_calls(self):
        stub = _StubProvider(["a", "b", "c"])
        with _patch_provider(stub):
            out1 = expand_module.expand_query("same query", n=3)
            out2 = expand_module.expand_query("same query", n=3)
            out3 = expand_module.expand_query("same query", n=3)
        assert out1 == out2 == out3
        assert len(stub.invoke_calls) == 1, "cache should suppress repeat LLM calls"

    def test_different_n_means_different_cache_key(self):
        stub = _StubProvider(["a", "b", "c"])
        with _patch_provider(stub):
            expand_module.expand_query("q", n=3)
            expand_module.expand_query("q", n=2)
        assert len(stub.invoke_calls) == 2, "different n must miss cache"

    def test_fail_open_on_provider_exception(self):
        stub = _StubProvider([], raise_exc=RuntimeError("provider down"))
        with _patch_provider(stub):
            out = expand_module.expand_query("the query", n=3)
        # Fail-open contract: caller gets the original query alone, no error.
        assert out == ["the query"]

    def test_dedupes_paraphrase_equal_to_original(self):
        # If the LLM returns a paraphrase identical to the original (after
        # strip), it should be dropped so the variants list isn't bloated.
        stub = _StubProvider(["original", "  original  ", "different"])
        with _patch_provider(stub):
            out = expand_module.expand_query("original", n=3)
        # Only the genuinely-different paraphrase survives
        assert out == ["original", "different"]

    def test_invalid_json_returns_original_only(self):
        """If the provider returns un-parseable JSON, expand should fail open."""

        class _BrokenProvider:
            def invoke(self, system, prompt, **kwargs):
                class _R:
                    text = "not valid json {{"
                return _R()

        with patch("agent.tools.llm_providers.resolve_provider", return_value=_BrokenProvider()):
            out = expand_module.expand_query("q", n=3)
        assert out == ["q"]

    def test_missing_paraphrases_key_returns_original_only(self):
        class _BadShape:
            def invoke(self, system, prompt, **kwargs):
                class _R:
                    text = json.dumps({"wrong_key": ["a", "b"]})
                return _R()

        with patch("agent.tools.llm_providers.resolve_provider", return_value=_BadShape()):
            out = expand_module.expand_query("q", n=3)
        assert out == ["q"]

    def test_explicit_provider_passes_to_resolve(self):
        stub = _StubProvider(["a", "b", "c"])
        with patch(
            "agent.tools.llm_providers.resolve_provider", return_value=stub
        ) as resolve:
            expand_module.expand_query("q", n=3, provider="codex")
        resolve.assert_called_once_with("codex")
