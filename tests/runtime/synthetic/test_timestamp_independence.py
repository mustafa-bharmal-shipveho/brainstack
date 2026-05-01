"""Synthetic timestamp-independence test.

Manifest output must be byte-identical regardless of host timezone, locale,
or current wall-clock. ts_ms is stored as an int and serialized as such;
no human-readable timestamp string is ever written by the runtime.

We never call time.time() inside the dataclasses themselves; the runtime is
responsible for stamping ts_ms at the call site, and tests pass it
explicitly. This test pins that contract.
"""
from __future__ import annotations

import os
from unittest import mock

from runtime.core.events import (
    EVENT_LOG_SCHEMA_VERSION,
    EventRecord,
    dump_event,
    load_event,
)
from runtime.core.manifest import (
    SCHEMA_VERSION,
    InjectionItemSnapshot,
    Manifest,
    dump_manifest,
    load_manifest,
)


def _make_event() -> EventRecord:
    return EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=1714512000000,
        event="Stop",
        session_id="tz-test",
        turn=5,
    )


def _make_manifest() -> Manifest:
    return Manifest(
        schema_version=SCHEMA_VERSION,
        turn=1,
        ts_ms=1714512000000,
        session_id="tz-test",
        budget_total=100,
        budget_used=10,
        items=[
            InjectionItemSnapshot(
                id="c-1",
                bucket="hot",
                source_path="p",
                sha256="0" * 64,
                token_count=10,
                retrieval_reason="r",
                last_touched_turn=1,
                pinned=False,
            ),
        ],
    )


def test_manifest_dump_independent_of_TZ() -> None:
    base = dump_manifest(_make_manifest())
    for tz in ["UTC", "America/Los_Angeles", "Asia/Tokyo", "Pacific/Apia"]:
        with mock.patch.dict(os.environ, {"TZ": tz}):
            assert dump_manifest(_make_manifest()) == base


def test_event_dump_independent_of_TZ() -> None:
    base = dump_event(_make_event())
    for tz in ["UTC", "America/Los_Angeles", "Asia/Tokyo", "Pacific/Apia"]:
        with mock.patch.dict(os.environ, {"TZ": tz}):
            assert dump_event(_make_event()) == base


def test_manifest_dump_independent_of_LC_ALL() -> None:
    base = dump_manifest(_make_manifest())
    for locale in ["C", "en_US.UTF-8", "de_DE.UTF-8", "ja_JP.UTF-8"]:
        with mock.patch.dict(os.environ, {"LC_ALL": locale, "LANG": locale}):
            assert dump_manifest(_make_manifest()) == base


def test_no_iso_timestamp_string_in_dump() -> None:
    """Reject any code path that converts ts_ms to a human string at dump time.
    ISO timestamps would tie the dump to a TZ; we want the integer."""
    out = dump_manifest(_make_manifest())
    assert "T" not in out or '"T' not in out  # crude: ISO has 'T' between date+time
    assert "2024-" not in out
    assert "2025-" not in out
    assert "2026-" not in out
