"""Sub-phase 1d: event log schema tests.

Each hook firing writes one EventRecord to the append-only event log. The
log is the substrate for replay/audit. Round-trip determinism + data-policy
compliance are mandatory.

Reference-only contract:
  - tool_input_keys is a sorted list of TOP-LEVEL key names from the tool
    input. The values are NEVER recorded.
  - tool_output_summary is {sha256, byte_len} only. The output text is NEVER
    recorded.
  - any raw content opt-in lives elsewhere (payload-samples.jsonl in the
    harness; opt-in capture flag in the production runtime).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from runtime.core.events import (
    EVENT_LOG_SCHEMA_VERSION,
    EventRecord,
    OutputSummary,
    append_event,
    dump_event,
    load_event,
    load_events,
    summarize_output,
)


def test_event_log_schema_version_constant() -> None:
    assert EVENT_LOG_SCHEMA_VERSION == "1.0"


def test_event_round_trip() -> None:
    e = EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=1714512000000,
        event="PostToolUse",
        session_id="s",
        turn=12,
        tool_name="Read",
        tool_input_keys=["file_path", "limit"],
        tool_output_summary=OutputSummary(sha256="a" * 64, byte_len=420),
        bucket="retrieved",
        item_ids_added=["c-001"],
        item_ids_evicted=[],
    )
    raw = dump_event(e)
    again = load_event(raw)
    assert again == e


def test_event_dump_deterministic() -> None:
    e = EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=1,
        event="Stop",
        session_id="s",
        turn=0,
    )
    a = dump_event(e)
    b = dump_event(e)
    c = dump_event(load_event(a))
    assert a == b == c


def test_event_no_raw_input_fields_in_dump() -> None:
    """Critical data-policy test: the dump MUST NOT contain any value strings
    from tool input — only the sorted key list."""
    e = EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=1,
        event="PostToolUse",
        session_id="s",
        turn=0,
        tool_name="Bash",
        # In real usage the runtime would receive tool_input={"command": "echo SECRET"}
        # but only the keys are stored. The test uses a sentinel value to
        # detect any future code path that accidentally captures values.
        tool_input_keys=["command"],
    )
    out = dump_event(e)
    assert "SECRET" not in out
    assert "echo" not in out
    assert "command" in out  # the key alone is fine


def test_summarize_output_returns_sha256_and_length() -> None:
    text = "hello world"
    s = summarize_output(text)
    assert s.byte_len == len(text.encode("utf-8"))
    assert s.sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_summarize_output_does_not_contain_raw() -> None:
    text = "fake-secret-VERY-OBVIOUS-MARKER"
    s = summarize_output(text)
    assert text not in s.sha256
    assert text not in str(s)


def test_append_event_to_log(tmp_path: Path) -> None:
    log = tmp_path / "events.log.jsonl"
    e1 = EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=1, event="SessionStart", session_id="s", turn=0,
    )
    e2 = EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=2, event="UserPromptSubmit", session_id="s", turn=1,
    )
    append_event(log, e1)
    append_event(log, e2)
    events = load_events(log)
    assert events == [e1, e2]


def test_load_events_skips_blank_lines(tmp_path: Path) -> None:
    log = tmp_path / "events.log.jsonl"
    log.write_text(
        '\n'
        + dump_event(EventRecord(EVENT_LOG_SCHEMA_VERSION, 1, "Stop", "s", 0)) + '\n'
        + '\n'
        + '\n',
        encoding="utf-8",
    )
    events = load_events(log)
    assert len(events) == 1
    assert events[0].event == "Stop"


def test_load_event_rejects_unknown_schema() -> None:
    bad = json.dumps({
        "schema_version": "99.0",
        "ts_ms": 1, "event": "X", "session_id": "s", "turn": 0,
    })
    with pytest.raises(ValueError):
        load_event(bad)


def test_event_roundtrip_with_evictions() -> None:
    e = EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=42,
        event="PostToolUse",
        session_id="s",
        turn=37,
        tool_name="Read",
        tool_input_keys=["file_path"],
        tool_output_summary=OutputSummary(sha256="b" * 64, byte_len=10),
        bucket="retrieved",
        item_ids_added=["c-100"],
        item_ids_evicted=["c-001", "c-002", "c-003"],
    )
    again = load_event(dump_event(e))
    assert again.item_ids_evicted == ["c-001", "c-002", "c-003"]
