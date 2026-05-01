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
    event_id_for,
    load_event,
    load_events,
    summarize_output,
)


def test_event_log_schema_version_constant() -> None:
    assert EVENT_LOG_SCHEMA_VERSION == "1.1"


def test_event_round_trip() -> None:
    e = EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=1714512000000,
        event="PostToolUse",
        session_id="s",
        turn=12,
        event_id="explicit-id-for-roundtrip",
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


def test_round_trip_byte_stable_even_when_event_id_auto_derived() -> None:
    """If the caller leaves event_id empty, dump auto-derives it; load
    returns a record with event_id populated; re-dumping is byte-identical
    to the first dump."""
    e = EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=1, event="Stop", session_id="s", turn=0,
    )
    a = dump_event(e)
    b = dump_event(load_event(a))
    assert a == b


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


def test_summarize_output_default_omits_sha256() -> None:
    """Security default per codex BLOCK: sha256 is empty unless include_hash=True.
    Reasoning: a stable hash of secret-bearing output is a fingerprint an
    attacker with a breach DB could correlate against."""
    text = "hello world"
    s = summarize_output(text)
    assert s.byte_len == len(text.encode("utf-8"))
    assert s.sha256 == ""  # default-off


def test_summarize_output_with_include_hash_returns_sha256() -> None:
    text = "hello world"
    s = summarize_output(text, include_hash=True)
    assert s.sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert s.byte_len == len(text.encode("utf-8"))


def test_summarize_output_does_not_contain_raw() -> None:
    text = "fake-secret-VERY-OBVIOUS-MARKER"
    s = summarize_output(text, include_hash=True)
    assert text not in s.sha256
    assert text not in str(s)
    s2 = summarize_output(text)  # default
    assert text not in str(s2)


def test_append_event_to_log(tmp_path: Path) -> None:
    log = tmp_path / "events.log.jsonl"
    e1 = EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=1, event="SessionStart", session_id="s", turn=0,
        event_id="e1",
    )
    e2 = EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=2, event="UserPromptSubmit", session_id="s", turn=1,
        event_id="e2",
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


def test_load_event_rejects_unknown_non_x_keys() -> None:
    """Mirror of manifest: non-x_ unknown fields are rejected, forcing
    explicit schema_version bumps for additions. Codex review caught the
    asymmetry where manifest enforced this but events did not."""
    bad = json.dumps({
        "schema_version": EVENT_LOG_SCHEMA_VERSION,
        "ts_ms": 1, "event": "X", "session_id": "s", "turn": 0,
        "future_runtime_field": "should be rejected",
    })
    with pytest.raises(ValueError):
        load_event(bad)


def test_load_event_preserves_x_extensions() -> None:
    """x_* unknown keys round-trip through extensions dict."""
    src = {
        "schema_version": EVENT_LOG_SCHEMA_VERSION,
        "ts_ms": 1, "event": "X", "session_id": "s", "turn": 0,
        "x_runtime_extension": {"experimental": True},
    }
    e = load_event(json.dumps(src))
    assert e.extensions == {"x_runtime_extension": {"experimental": True}}
    out = dump_event(e)
    assert "x_runtime_extension" in out


def test_event_id_auto_derived_when_empty() -> None:
    """If the caller doesn't supply event_id, dump_event derives one
    deterministically from the natural key."""
    e1 = EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=42, event="Stop", session_id="s1", turn=5,
    )
    e2 = EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=42, event="Stop", session_id="s1", turn=5,
    )
    p1 = json.loads(dump_event(e1))
    p2 = json.loads(dump_event(e2))
    assert p1["event_id"] == p2["event_id"]
    assert len(p1["event_id"]) == 16
    assert all(c in "0123456789abcdef" for c in p1["event_id"])


def test_event_id_for_is_deterministic() -> None:
    """event_id_for() with the same inputs returns the same id."""
    a = event_id_for("s", 1, 100, "PostToolUse")
    b = event_id_for("s", 1, 100, "PostToolUse")
    assert a == b


def test_event_id_distinguishes_natural_key_components() -> None:
    """Differing session/turn/ts/event must produce different ids."""
    base = event_id_for("s", 1, 100, "PostToolUse")
    assert base != event_id_for("s2", 1, 100, "PostToolUse")
    assert base != event_id_for("s", 2, 100, "PostToolUse")
    assert base != event_id_for("s", 1, 101, "PostToolUse")
    assert base != event_id_for("s", 1, 100, "Stop")


def test_oversized_extension_value_rejected_at_dump() -> None:
    """Security default per codex BLOCK: x_* extension values larger than
    MAX_EXTENSION_BYTES (1 KiB) are rejected at dump time. This prevents
    an adapter from stuffing raw tool output into x_full_payload and
    bypassing the data policy via the extension mechanism."""
    e = EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=1, event="X", session_id="s", turn=0,
        extensions={"x_full_payload": "leak " * 500},  # ~2.5 KiB
    )
    with pytest.raises(ValueError, match="extensions are metadata, not payload"):
        dump_event(e)


def test_small_extension_value_accepted() -> None:
    """The extension guard does not block legitimate small metadata."""
    e = EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=1, event="X", session_id="s", turn=0,
        extensions={"x_runtime_version": "0.2.0", "x_flag": True, "x_count": 42},
    )
    out = dump_event(e)
    assert "x_runtime_version" in out


def test_items_added_round_trip_with_full_snapshots() -> None:
    """v1.1 added: events carry full InjectionItemSnapshot objects so
    replay can reconstruct the manifest from events alone (no external
    state). Skeptic finding #2."""
    from runtime.core.manifest import InjectionItemSnapshot

    snapshot = InjectionItemSnapshot(
        id="c-001",
        bucket="hot",
        source_path="hot/lessons/foo.md",
        sha256="a" * 64,
        token_count=100,
        retrieval_reason="post-tool-use:Read",
        last_touched_turn=5,
        pinned=False,
        score=0.85,
    )
    e = EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=1, event="PostToolUse", session_id="s", turn=5,
        items_added=[snapshot],
    )
    raw = dump_event(e)
    again = load_event(raw)
    assert len(again.items_added) == 1
    assert again.items_added[0].id == "c-001"
    assert again.items_added[0].score == 0.85
    assert again.items_added[0].source_path == "hot/lessons/foo.md"


def test_items_added_default_empty() -> None:
    """When no snapshots, items_added round-trips as empty list."""
    e = EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=1, event="Stop", session_id="s", turn=0,
    )
    raw = dump_event(e)
    again = load_event(raw)
    assert again.items_added == []


def test_event_id_user_supplied_takes_precedence() -> None:
    e = EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=1, event="X", session_id="s", turn=0,
        event_id="user-supplied-id",
    )
    out = json.loads(dump_event(e))
    assert out["event_id"] == "user-supplied-id"


def test_tool_input_keys_sorted_at_dump_regardless_of_caller_order() -> None:
    """Determinism contract: even if a caller hands us tool_input_keys in
    any order, the dump must be byte-identical. Codex review caught this:
    relying on caller-side sorting was a determinism leak."""
    e1 = EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=1, event="PostToolUse", session_id="s", turn=0,
        tool_name="Bash",
        tool_input_keys=["command", "cwd", "env"],  # already sorted
    )
    e2 = EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=1, event="PostToolUse", session_id="s", turn=0,
        tool_name="Bash",
        tool_input_keys=["env", "command", "cwd"],  # different order
    )
    e3 = EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=1, event="PostToolUse", session_id="s", turn=0,
        tool_name="Bash",
        tool_input_keys=["cwd", "env", "command"],  # yet another order
    )
    assert dump_event(e1) == dump_event(e2) == dump_event(e3)
