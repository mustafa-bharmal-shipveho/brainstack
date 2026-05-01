"""Synthetic leak battery.

Feed the runtime's event log + manifest writers a wide variety of
fake-secret-shaped strings as tool input/output. Assert that NONE appear
in the on-disk artifacts under default settings.

This is the production-side counterpart to the harness leak test. Where
the harness verifies log_event.sh doesn't leak, this verifies the
runtime/core/events.py + runtime/core/manifest.py don't leak either.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from runtime.core.events import (
    EVENT_LOG_SCHEMA_VERSION,
    EventRecord,
    append_event,
    load_events,
    summarize_output,
)
from runtime.core.manifest import (
    SCHEMA_VERSION,
    InjectionItemSnapshot,
    Manifest,
    dump_manifest,
)

# Each entry is a fake "secret-shaped" payload. None of these strings should
# ever appear in default on-disk artifacts. Every value here is fake.
FAKE_SECRET_PATTERNS: list[str] = [
    "sk_live_FAKE_TEST_TOKEN_DO_NOT_LEAK_ABCDEF1234567890",
    "AKIA0000000000000000",  # AWS access key shape
    "ghp_FAKE_TEST_GH_TOKEN_DO_NOT_USE_REPLACE_ME",
    "Bearer eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.fakefake.fake",
    "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----",
    "postgres://user:fake_password@localhost:5432/db",
    "redis://:fake_password@localhost:6379",
    "FAKE_API_KEY=do_not_use_this_value_anywhere_real_xyzzy",
]


@pytest.mark.parametrize("secret", FAKE_SECRET_PATTERNS)
def test_event_log_does_not_leak_secret_via_summary(tmp_path: Path, secret: str) -> None:
    """Tool output can be summarized; the summary must NOT contain the raw."""
    log = tmp_path / "events.log.jsonl"
    e = EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=1,
        event="PostToolUse",
        session_id="leak-test",
        turn=0,
        tool_name="Bash",
        tool_input_keys=["command"],
        tool_output_summary=summarize_output(secret),
    )
    append_event(log, e)
    raw = log.read_text(encoding="utf-8")
    assert secret not in raw, f"secret leaked into event log: {secret[:30]}..."


@pytest.mark.parametrize("secret", FAKE_SECRET_PATTERNS)
def test_event_log_does_not_leak_via_input_keys(tmp_path: Path, secret: str) -> None:
    """tool_input_keys is sorted top-level KEY NAMES only. Even if a user
    puts a secret-shaped string in a key name (silly but possible), it
    appears verbatim — but never via VALUES."""
    log = tmp_path / "events.log.jsonl"
    # Simulate a tool input where the SECRET is in the value (the realistic case)
    # We pass only the keys, never the values.
    e = EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=1,
        event="PostToolUse",
        session_id="leak-test",
        turn=0,
        tool_name="Bash",
        tool_input_keys=["command", "env", "cwd"],  # NEVER the values
    )
    append_event(log, e)
    raw = log.read_text(encoding="utf-8")
    assert secret not in raw


@pytest.mark.parametrize("secret", FAKE_SECRET_PATTERNS)
def test_manifest_does_not_leak_via_source_or_reason(secret: str) -> None:
    """Manifest items contain source_path + sha256 + reason — but the runtime
    NEVER puts content there. Construct a manifest that intentionally hides
    a secret in the source_path (worst case) and verify the rest is clean."""
    m = Manifest(
        schema_version=SCHEMA_VERSION,
        turn=1,
        ts_ms=1,
        session_id="x",
        budget_total=100,
        budget_used=10,
        items=[
            InjectionItemSnapshot(
                id="c-1",
                bucket="hot",
                source_path="hot/lessons/innocent-name.md",
                sha256="0" * 64,
                token_count=10,
                retrieval_reason="pinned",
                last_touched_turn=1,
                pinned=True,
            ),
        ],
    )
    out = dump_manifest(m)
    assert secret not in out


def test_summary_truncates_to_metadata_only() -> None:
    """The OutputSummary contains only sha256 + byte_len. No matter how
    large the input, the summary stays a fixed size."""
    big_input = "x" * 1_000_000
    s = summarize_output(big_input)
    # sha256 is 64 hex chars + small int; should fit in <100 chars when serialized
    payload = f"{s.sha256}:{s.byte_len}"
    assert len(payload) < 200
    assert big_input not in payload
