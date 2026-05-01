"""Phase 4a: Claude Code adapter hook tests.

handle_hook() is the seam: it reads stdin JSON, builds an EventRecord,
appends to the configured log. No engine work happens here — the adapter
is append-only by design.
"""
from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path

import pytest

from runtime.adapters.claude_code.config import RuntimeConfig
from runtime.adapters.claude_code.hooks import handle_hook
from runtime.core.events import load_events


@pytest.fixture
def tmp_config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(log_dir=tmp_path / "logs")


@pytest.fixture
def stdin_with(monkeypatch):
    """Inject a stdin payload."""
    def _set(payload: object) -> None:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        monkeypatch.setattr(sys, "stdin", StringIO(text))
    return _set


# ---------- routing ----------

def test_unknown_event_is_noop(tmp_config: RuntimeConfig, stdin_with) -> None:
    stdin_with({"session_id": "s"})
    rc = handle_hook("CompletelyUnknownEventXYZ", config=tmp_config)
    assert rc == 0
    assert not tmp_config.event_log_path.exists()


def test_session_start_writes_event(tmp_config: RuntimeConfig, stdin_with) -> None:
    stdin_with({"session_id": "s-1"})
    rc = handle_hook("SessionStart", config=tmp_config)
    assert rc == 0
    events = load_events(tmp_config.event_log_path)
    assert len(events) == 1
    assert events[0].event == "SessionStart"
    assert events[0].session_id == "s-1"


def test_user_prompt_submit(tmp_config: RuntimeConfig, stdin_with) -> None:
    stdin_with({"session_id": "s"})
    handle_hook("UserPromptSubmit", config=tmp_config)
    events = load_events(tmp_config.event_log_path)
    assert events[0].event == "UserPromptSubmit"


def test_stop_writes_event(tmp_config: RuntimeConfig, stdin_with) -> None:
    stdin_with({"session_id": "s"})
    handle_hook("Stop", config=tmp_config)
    events = load_events(tmp_config.event_log_path)
    assert events[0].event == "Stop"


# ---------- PostToolUse content handling ----------

def test_post_tool_use_read_creates_item(tmp_config: RuntimeConfig, stdin_with) -> None:
    stdin_with({
        "session_id": "s",
        "tool_name": "Read",
        "tool_input": {"file_path": "/tmp/foo.md"},
        "tool_response": "the quick brown fox " * 50,
    })
    handle_hook("PostToolUse", config=tmp_config)
    events = load_events(tmp_config.event_log_path)
    assert len(events) == 1
    assert events[0].event == "PostToolUse"
    assert events[0].tool_name == "Read"
    assert len(events[0].items_added) == 1
    snap = events[0].items_added[0]
    assert snap.bucket == "retrieved"
    assert snap.token_count > 0
    assert "/tmp/foo.md" in snap.source_path


def test_post_tool_use_unknown_tool_skipped(tmp_config: RuntimeConfig, stdin_with) -> None:
    stdin_with({
        "session_id": "s",
        "tool_name": "RandomTool",
        "tool_response": "anything",
    })
    handle_hook("PostToolUse", config=tmp_config)
    events = load_events(tmp_config.event_log_path)
    assert events[0].items_added == []  # not Read/Glob/Grep/Bash/Edit/Write -> no item


def test_post_tool_use_edit_goes_to_scratchpad(tmp_config: RuntimeConfig, stdin_with) -> None:
    stdin_with({
        "session_id": "s",
        "tool_name": "Edit",
        "tool_input": {"file_path": "src.py", "old_string": "a", "new_string": "b"},
        "tool_response": "edited content here " * 20,
    })
    handle_hook("PostToolUse", config=tmp_config)
    events = load_events(tmp_config.event_log_path)
    assert events[0].items_added[0].bucket == "scratchpad"


def test_post_tool_use_no_response_no_item(tmp_config: RuntimeConfig, stdin_with) -> None:
    stdin_with({
        "session_id": "s",
        "tool_name": "Read",
        "tool_input": {"file_path": "/tmp/foo.md"},
        "tool_response": "",
    })
    handle_hook("PostToolUse", config=tmp_config)
    events = load_events(tmp_config.event_log_path)
    assert events[0].items_added == []  # empty content -> no snapshot


# ---------- determinism ----------

def test_same_content_same_id(tmp_config: RuntimeConfig, stdin_with) -> None:
    """Identical content from the same tool produces the same id (allows
    Engine to deduplicate via re-add semantics)."""
    payload = {
        "session_id": "s",
        "tool_name": "Read",
        "tool_input": {"file_path": "/tmp/foo.md"},
        "tool_response": "deterministic content " * 30,
    }
    stdin_with(payload)
    handle_hook("PostToolUse", config=tmp_config)
    stdin_with(payload)
    handle_hook("PostToolUse", config=tmp_config)
    events = load_events(tmp_config.event_log_path)
    assert len(events) == 2
    assert events[0].items_added[0].id == events[1].items_added[0].id


def test_event_record_has_no_raw_response_text(tmp_config: RuntimeConfig, stdin_with) -> None:
    """Data policy: raw tool_response text MUST NOT appear in events.jsonl."""
    secret_marker = "FAKE-SENTINEL-DO-NOT-LEAK-ZXCVBN-99887"
    stdin_with({
        "session_id": "s",
        "tool_name": "Bash",
        "tool_input": {"command": "echo hi"},
        "tool_response": f"the output contains {secret_marker} which must not leak",
    })
    handle_hook("PostToolUse", config=tmp_config)
    raw = tmp_config.event_log_path.read_text()
    assert secret_marker not in raw
    # And the metadata-only summary should still be there
    assert "tool_output_summary" in raw


# ---------- config ----------

def test_runtime_config_defaults() -> None:
    cfg = RuntimeConfig()
    assert cfg.log_dir.name == "logs"
    assert cfg.capture_raw is False
    assert cfg.budgets["claude_md"] == 4000
    assert cfg.tool_to_bucket("Read") == "retrieved"
    assert cfg.tool_to_bucket("Edit") == "scratchpad"
    assert cfg.tool_to_bucket("UnknownTool") == "retrieved"


def test_runtime_config_loads_from_pyproject(tmp_path: Path) -> None:
    cfg_path = tmp_path / "pyproject.toml"
    cfg_path.write_text(
        '[tool.recall.runtime]\n'
        'log_dir = "/tmp/runtime-test-logs"\n'
        'capture_raw = true\n'
        '[tool.recall.runtime.budget]\n'
        'claude_md = 999\n'
        'retrieved = 12345\n'
    )
    cfg = RuntimeConfig.load(config_path=cfg_path)
    assert cfg.log_dir == Path("/tmp/runtime-test-logs")
    assert cfg.capture_raw is True
    assert cfg.budgets["claude_md"] == 999
    assert cfg.budgets["retrieved"] == 12345
    # Unspecified budgets fall back to defaults
    assert cfg.budgets["hot"] == 2000


def test_runtime_config_missing_file_returns_defaults(tmp_path: Path) -> None:
    cfg = RuntimeConfig.load(config_path=tmp_path / "does-not-exist.toml")
    assert cfg.budgets["claude_md"] == 4000
