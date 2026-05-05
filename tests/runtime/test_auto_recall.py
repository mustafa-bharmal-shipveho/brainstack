"""Auto-recall hook + module tests.

When `RuntimeConfig.enable_auto_recall` is True, the UserPromptSubmit hook
fires recall and injects top-K results into Claude Code's context as a
<system-reminder> block. The hook branch lives at hooks.py:170-ish (sibling
to the existing reinjection branch).

Tests cover:
- skip filter (short prompt, slash command, bareword ack)
- happy path: query runs, block emitted to stdout, AutoRecall telemetry event written
- timeout: builder takes longer than timeout_ms → no stdout, outcome=timeout in telemetry
- unavailable: retriever raises (e.g., qdrant not installed) → no stdout, outcome=unavailable
- composition with reinjection: both flags True → two blocks emitted
- disabled: enable_auto_recall=False → no auto-recall block, base telemetry still written
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from runtime.adapters.claude_code.config import RuntimeConfig
from runtime.adapters.claude_code.hooks import handle_hook
from runtime.core.events import load_events


# ---------- shared fixtures ----------

@pytest.fixture
def tmp_config(tmp_path: Path) -> RuntimeConfig:
    """Default RuntimeConfig with auto-recall ENABLED. Tests that need it
    off override `enable_auto_recall=False` per case."""
    return RuntimeConfig(
        log_dir=tmp_path / "logs",
        enable_auto_recall=True,
        auto_recall_k=5,
        auto_recall_budget_tokens=1500,
        auto_recall_timeout_ms=1500,
        auto_recall_min_chars=8,
    )


@pytest.fixture
def stdin_with(monkeypatch):
    def _set(payload: object) -> None:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        monkeypatch.setattr(sys, "stdin", StringIO(text))
    return _set


@dataclass
class _FakeQueryResult:
    """Duck-typed stand-in for recall.core.QueryResult — only fields the
    auto_recall block-builder actually reads."""
    path: str
    source: str
    name: str
    score: float
    body: str = ""


class _FakeRetriever:
    """Minimal retriever: returns canned results, optionally simulating
    latency or failure modes."""

    def __init__(self, results: list[_FakeQueryResult] | None = None,
                 sleep_seconds: float = 0.0,
                 raises: type[Exception] | None = None):
        self._results = results or []
        self._sleep = sleep_seconds
        self._raises = raises
        self.calls: list[tuple[str, int]] = []

    def query(self, prompt: str, *, k: int = 5,
              type_filter: Any = None, source_filter: Any = None):
        self.calls.append((prompt, k))
        if self._sleep:
            time.sleep(self._sleep)
        if self._raises:
            raise self._raises("simulated retrieval failure")
        return self._results[:k]


# ---------- should_skip unit tests ----------

class TestShouldSkip:
    def test_short_prompt_skipped(self):
        from runtime.adapters.claude_code.auto_recall import should_skip
        skip, reason = should_skip("hi", min_chars=8)
        assert skip is True
        assert reason == "too_short"

    def test_at_min_chars_not_skipped(self):
        from runtime.adapters.claude_code.auto_recall import should_skip
        skip, reason = should_skip("a" * 8, min_chars=8)
        assert skip is False
        assert reason is None

    def test_slash_command_skipped(self):
        from runtime.adapters.claude_code.auto_recall import should_skip
        skip, reason = should_skip("/clear", min_chars=4)
        assert skip is True
        assert reason == "slash"

    def test_bareword_ack_skipped(self):
        from runtime.adapters.claude_code.auto_recall import should_skip
        for word in ["yes", "ok", "done", "nope", "OK!", "Yep.", "ty"]:
            skip, reason = should_skip(word, min_chars=2)
            assert skip is True, f"expected skip on {word!r}"
            assert reason == "ack"

    def test_normal_question_not_skipped(self):
        from runtime.adapters.claude_code.auto_recall import should_skip
        skip, _ = should_skip("what do I do during an incident?", min_chars=8)
        assert skip is False


# ---------- build_recall_block unit tests ----------

class TestBuildRecallBlock:
    """The block builder takes a retriever + prompt and produces:
    1. A <system-reminder> string for stdout (or '' if no results)
    2. A telemetry dict for the AutoRecall event

    Tests pin both the structure of the rendered block and the telemetry
    schema, since downstream `recall stats` depends on the latter."""

    def test_renders_metadata_header_and_excerpts(self):
        from runtime.adapters.claude_code.auto_recall import build_recall_block
        retr = _FakeRetriever(results=[
            _FakeQueryResult(
                path="/brain/imports/kb/key-contacts.md",
                source="imports", name="key-contacts", score=0.84,
                body="# Key Contacts\n\nMike: head of platform.\n",
            ),
            _FakeQueryResult(
                path="/brain/memory/semantic/lessons/feedback.md",
                source="brain", name="feedback", score=0.71,
                body="Always lead with executable artifact.",
            ),
        ])
        block, telemetry = build_recall_block(
            "who is the head of platform?", retr, k=5, budget_tokens=1500
        )
        # Block must be a <system-reminder> wrapping the metadata + excerpts
        assert block.startswith("<system-reminder>")
        assert block.rstrip().endswith("</system-reminder>")
        # Metadata header reveals docs surfaced + top scores (rounded to 2dp)
        assert "auto-recall: 2 docs" in block
        assert "0.84" in block
        # Per-doc sections include path + score
        assert "/brain/imports/kb/key-contacts.md" in block
        assert "0.71" in block
        # Note about score semantics, addressing over-reliance worry
        assert "scores are retrieval similarity" in block.lower()
        # Telemetry shape (for `recall stats`)
        assert telemetry["x_outcome"] == "hit"
        assert telemetry["x_k_returned"] == 2
        assert telemetry["x_k_requested"] == 5
        assert telemetry["x_top_scores"] == [0.84, 0.71]
        assert telemetry["x_sources"] == {"imports": 1, "brain": 1}

    def test_empty_results_emit_no_block(self):
        from runtime.adapters.claude_code.auto_recall import build_recall_block
        block, telemetry = build_recall_block(
            "obscure query with no hits", _FakeRetriever(results=[]),
            k=5, budget_tokens=1500,
        )
        assert block == ""
        assert telemetry["x_outcome"] == "hit"
        assert telemetry["x_k_returned"] == 0

    def test_budget_truncates_excerpts(self):
        """When the running token total exceeds budget_tokens, later docs
        are skipped. Pins the contract: rendered block stays within a small
        multiplier of the budget, regardless of how many results are passed
        in."""
        from runtime.adapters.claude_code.auto_recall import build_recall_block
        from runtime.core.tokens import OfflineTokenCounter

        long_body = "x" * 10000  # ~2500 tokens worth of content per doc
        retr = _FakeRetriever(results=[
            _FakeQueryResult(path=f"/p{i}.md", source="brain",
                             name=f"d{i}", score=0.9 - i * 0.1, body=long_body)
            for i in range(5)
        ])
        budget = 500
        block, _ = build_recall_block("x", retr, k=5, budget_tokens=budget)
        # The rendered block must stay within a small multiplier of the
        # budget. Allow 3x slack for the header + one over-budget section
        # being included before truncation kicks in.
        rendered_tokens = OfflineTokenCounter().count(block)
        assert rendered_tokens <= budget * 3, (
            f"block rendered {rendered_tokens} tokens, budget was {budget}"
        )

    def test_min_score_filters_low_relevance_hits(self):
        """When auto_recall_min_score is set, results below the floor are
        dropped before injection. Default (0.0) keeps everything — pins the
        backward-compat path. Codex 2026-05-05 MED."""
        from runtime.adapters.claude_code.auto_recall import build_recall_block
        retr = _FakeRetriever(results=[
            _FakeQueryResult(path="/strong.md", source="brain", name="s",
                             score=0.85, body="strong match"),
            _FakeQueryResult(path="/weak.md", source="brain", name="w",
                             score=0.20, body="weak match"),
        ])
        # Default: both included
        block_default, telem_default = build_recall_block(
            "x", retr, k=5, budget_tokens=1500,
        )
        assert "/strong.md" in block_default
        assert "/weak.md" in block_default
        assert telem_default["x_k_returned"] == 2

        # With floor: only the strong match survives
        block_filtered, telem_filtered = build_recall_block(
            "x", retr, k=5, budget_tokens=1500, min_score=0.5,
        )
        assert "/strong.md" in block_filtered
        assert "/weak.md" not in block_filtered
        assert telem_filtered["x_k_returned"] == 1

    def test_telemetry_within_extension_size_cap(self):
        """events.py enforces MAX_EXTENSION_BYTES=1024 per x_* value. Verify
        no single key ever exceeds that — keeps logs writable forever."""
        from runtime.adapters.claude_code.auto_recall import build_recall_block
        retr = _FakeRetriever(results=[
            _FakeQueryResult(path=f"/long-path-{'x'*200}.md", source=f"src{i}",
                             name=f"d{i}", score=0.5, body="x")
            for i in range(20)
        ])
        _, telemetry = build_recall_block("x", retr, k=20, budget_tokens=99999)
        for k, v in telemetry.items():
            encoded = json.dumps(v).encode("utf-8")
            assert len(encoded) <= 1024, f"telemetry[{k}] is {len(encoded)} bytes"


# ---------- hook integration tests ----------

class TestPyprojectDiscovery:
    """RuntimeConfig.load() should find the pyproject that owns the
    [tool.recall.runtime] section, not just the first existing
    pyproject.toml in the search path. Without this, a user with
    `enable_auto_recall = true` in ~/.agent/runtime/pyproject.toml gets
    the feature silently disabled when running inside any project repo
    whose pyproject.toml has no [tool.recall.runtime] section. Codex
    2026-05-05 MED."""

    def test_falls_through_when_cwd_pyproject_lacks_runtime_section(
        self, tmp_path: Path, monkeypatch
    ):
        """cwd/pyproject.toml exists but has no [tool.recall.runtime] →
        load() should skip it and pick up the global ~/.agent file."""
        # Set up a fake cwd with a pyproject that has nothing relevant
        cwd = tmp_path / "project"
        cwd.mkdir()
        (cwd / "pyproject.toml").write_text(
            "[build-system]\nrequires = [\"hatchling\"]\n"
        )
        # Set up a fake ~/.agent/runtime/pyproject.toml with the section
        fake_home = tmp_path / "home"
        agent_runtime = fake_home / ".agent" / "runtime"
        agent_runtime.mkdir(parents=True)
        (agent_runtime / "pyproject.toml").write_text(
            "[tool.recall.runtime]\n"
            "enable_auto_recall = true\n"
        )
        monkeypatch.chdir(cwd)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
        monkeypatch.delenv("RECALL_RUNTIME_CONFIG", raising=False)

        cfg = RuntimeConfig.load()
        assert cfg.enable_auto_recall is True
        assert "agent/runtime" in str(cfg.config_path)

    def test_uses_cwd_when_it_has_runtime_section(
        self, tmp_path: Path, monkeypatch
    ):
        """Per-project override still works: if cwd's pyproject has
        [tool.recall.runtime], it takes precedence over ~/.agent."""
        cwd = tmp_path / "project"
        cwd.mkdir()
        (cwd / "pyproject.toml").write_text(
            "[tool.recall.runtime]\n"
            "enable_auto_recall = false\n"
        )
        fake_home = tmp_path / "home"
        agent_runtime = fake_home / ".agent" / "runtime"
        agent_runtime.mkdir(parents=True)
        (agent_runtime / "pyproject.toml").write_text(
            "[tool.recall.runtime]\nenable_auto_recall = true\n"
        )
        monkeypatch.chdir(cwd)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
        monkeypatch.delenv("RECALL_RUNTIME_CONFIG", raising=False)

        cfg = RuntimeConfig.load()
        # cwd's `false` wins over ~/.agent's `true`
        assert cfg.enable_auto_recall is False


class TestHookIntegration:
    """End-to-end through `handle_hook("UserPromptSubmit", ...)`. These
    pin the full path: stdin payload → skip filter → retriever → stdout +
    AutoRecall event written."""

    def _patch_retriever(self, monkeypatch, retriever):
        """Replace the retriever factory the hook uses with a stub."""
        import runtime.adapters.claude_code.auto_recall as ar_mod
        monkeypatch.setattr(ar_mod, "_load_retriever", lambda: retriever)

    def test_disabled_emits_no_auto_recall_block(
        self, tmp_path: Path, stdin_with, monkeypatch, capsys
    ):
        cfg = RuntimeConfig(log_dir=tmp_path / "logs", enable_auto_recall=False)
        stdin_with({"session_id": "s", "prompt": "what is the incident protocol?"})
        rc = handle_hook("UserPromptSubmit", config=cfg)
        assert rc == 0
        captured = capsys.readouterr()
        assert "auto-recall:" not in captured.out
        # Base UserPromptSubmit telemetry IS written, just not AutoRecall
        events = load_events(cfg.event_log_path)
        assert {e.event for e in events} == {"UserPromptSubmit"}

    def test_short_prompt_writes_skip_telemetry(
        self, tmp_config: RuntimeConfig, stdin_with, monkeypatch, capsys
    ):
        retr = _FakeRetriever(results=[])
        self._patch_retriever(monkeypatch, retr)
        stdin_with({"session_id": "s", "prompt": "hi"})
        handle_hook("UserPromptSubmit", config=tmp_config)
        captured = capsys.readouterr()
        assert "auto-recall:" not in captured.out
        # Retriever was NOT called (skip happens first)
        assert retr.calls == []
        # Telemetry: AutoRecall event with outcome=skip
        events = load_events(tmp_config.event_log_path)
        ar = [e for e in events if e.event == "AutoRecall"]
        assert len(ar) == 1
        assert ar[0].extensions.get("x_outcome") == "skip"
        assert ar[0].extensions.get("x_skip_reason") == "too_short"

    def test_happy_path_emits_block_and_telemetry(
        self, tmp_config: RuntimeConfig, stdin_with, monkeypatch, capsys
    ):
        retr = _FakeRetriever(results=[
            _FakeQueryResult(path="/brain/lesson.md", source="brain",
                             name="lesson", score=0.9, body="Lesson body."),
        ])
        self._patch_retriever(monkeypatch, retr)
        stdin_with({"session_id": "s", "prompt": "what is the incident protocol?"})
        handle_hook("UserPromptSubmit", config=tmp_config)
        captured = capsys.readouterr()
        # Block was emitted to stdout (Claude Code reads + injects)
        assert "<system-reminder>" in captured.out
        assert "auto-recall:" in captured.out
        assert "/brain/lesson.md" in captured.out
        # Retriever called with the prompt
        assert retr.calls == [("what is the incident protocol?", 5)]
        # AutoRecall event written
        events = load_events(tmp_config.event_log_path)
        ar = [e for e in events if e.event == "AutoRecall"]
        assert len(ar) == 1
        assert ar[0].extensions.get("x_outcome") == "hit"
        assert ar[0].extensions.get("x_k_returned") == 1

    def test_timeout_emits_no_block_telemetry_records_outcome(
        self, tmp_path: Path, stdin_with, monkeypatch, capsys
    ):
        # Tight timeout to keep the test fast
        cfg = RuntimeConfig(
            log_dir=tmp_path / "logs",
            enable_auto_recall=True,
            auto_recall_timeout_ms=50,
            auto_recall_min_chars=4,
        )
        retr = _FakeRetriever(
            results=[_FakeQueryResult(path="/p.md", source="brain",
                                      name="p", score=0.9, body="x")],
            sleep_seconds=0.5,  # 500ms > 50ms timeout
        )
        self._patch_retriever(monkeypatch, retr)
        stdin_with({"session_id": "s", "prompt": "long enough prompt"})
        handle_hook("UserPromptSubmit", config=cfg)
        captured = capsys.readouterr()
        assert "auto-recall:" not in captured.out
        events = load_events(cfg.event_log_path)
        ar = [e for e in events if e.event == "AutoRecall"]
        assert len(ar) == 1
        assert ar[0].extensions.get("x_outcome") == "timeout"

    def test_retriever_unavailable_fails_open(
        self, tmp_config: RuntimeConfig, stdin_with, monkeypatch, capsys
    ):
        """ImportError or any exception loading the retriever → no block,
        no crash, telemetry records outcome=unavailable. Most likely cause
        is qdrant_client/fastembed not installed; user gets diagnostics
        via `recall doctor` not by every prompt blowing up."""
        import runtime.adapters.claude_code.auto_recall as ar_mod

        def _broken_loader():
            raise ImportError("qdrant_client not installed")

        monkeypatch.setattr(ar_mod, "_load_retriever", _broken_loader)
        stdin_with({"session_id": "s", "prompt": "what is the incident protocol?"})
        rc = handle_hook("UserPromptSubmit", config=tmp_config)
        assert rc == 0  # never raise
        captured = capsys.readouterr()
        assert "auto-recall:" not in captured.out
        events = load_events(tmp_config.event_log_path)
        ar = [e for e in events if e.event == "AutoRecall"]
        assert len(ar) == 1
        assert ar[0].extensions.get("x_outcome") == "unavailable"

    def test_composes_with_reinjection(
        self, tmp_path: Path, stdin_with, monkeypatch, capsys
    ):
        """Both `enable_reinjection=True` and `enable_auto_recall=True` →
        the auto-recall block appears in stdout AND the AutoRecall event
        is logged, regardless of whether reinjection itself emitted
        anything (which depends on prior session state — empty event log
        means no reinjection block, but auto-recall must still fire)."""
        cfg = RuntimeConfig(
            log_dir=tmp_path / "logs",
            enable_reinjection=True,
            enable_auto_recall=True,
            auto_recall_min_chars=4,
        )
        retr = _FakeRetriever(results=[
            _FakeQueryResult(path="/p.md", source="brain", name="p",
                             score=0.9, body="recall body"),
        ])
        self._patch_retriever(monkeypatch, retr)
        stdin_with({"session_id": "s", "prompt": "long enough prompt here"})
        handle_hook("UserPromptSubmit", config=cfg)
        captured = capsys.readouterr()
        # Auto-recall block must be in stdout
        assert "auto-recall:" in captured.out
        assert "/p.md" in captured.out
        # AutoRecall event must be logged (proves the branch ran fully,
        # not just that something landed in stdout)
        events = load_events(cfg.event_log_path)
        ar = [e for e in events if e.event == "AutoRecall"]
        assert len(ar) == 1
        assert ar[0].extensions.get("x_outcome") == "hit"
