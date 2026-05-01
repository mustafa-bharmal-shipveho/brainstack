"""Sub-phase 1a (partial): manifest schema contract tests.

The manifest is the runtime's primary artifact. It is the contract between
the runtime and any consumer (replay, audit, CLI). Schema version 1.0 is
locked here. Tool-event-specific fields are TBD pending sub-phase 0b.

Round-trip determinism is critical: same manifest in -> same JSON out, byte
for byte. Tested with `sort_keys=True, ensure_ascii=False, separators=(",", ":")`.
"""
from __future__ import annotations

import json

import pytest

from runtime.core.manifest import (
    SCHEMA_VERSION,
    InjectionItemSnapshot,
    Manifest,
    SchemaVersionError,
    dump_manifest,
    load_manifest,
)


def _sample() -> Manifest:
    return Manifest(
        schema_version=SCHEMA_VERSION,
        turn=37,
        ts_ms=1714512000000,
        session_id="sess-abc-123",
        budget_total=180_000,
        budget_used=18_420,
        items=[
            InjectionItemSnapshot(
                id="c-000091",
                bucket="hot",
                source_path="hot/lessons/postgres-locking.md",
                sha256="a" * 64,
                token_count=412,
                retrieval_reason="pinned",
                last_touched_turn=37,
                pinned=True,
            ),
            InjectionItemSnapshot(
                id="c-000147",
                bucket="retrieved",
                source_path="retrieved/turn-6-fix-summary.md",
                sha256="b" * 64,
                token_count=280,
                retrieval_reason="post-tool-use:Read",
                last_touched_turn=6,
                pinned=False,
            ),
        ],
    )


# ---------- round-trip ----------

def test_round_trip_byte_identical() -> None:
    """Critical: dumping and re-dumping must produce the exact same bytes.

    Without this, golden fixtures churn on every run."""
    m = _sample()
    a = dump_manifest(m)
    b = dump_manifest(load_manifest(a))
    c = dump_manifest(load_manifest(b))
    assert a == b == c


def test_dump_is_sorted_and_compact() -> None:
    m = _sample()
    out = dump_manifest(m)
    # Must use sorted keys (deterministic across dict-insertion-order changes)
    parsed = json.loads(out)
    assert list(parsed.keys()) == sorted(parsed.keys())
    # No insignificant whitespace creeping in
    assert "\n " not in out


def test_load_then_dump_does_not_grow() -> None:
    m = _sample()
    out1 = dump_manifest(m)
    out2 = dump_manifest(load_manifest(out1))
    assert len(out1) == len(out2)


# ---------- schema version ----------

def test_dump_includes_schema_version() -> None:
    m = _sample()
    parsed = json.loads(dump_manifest(m))
    assert parsed["schema_version"] == SCHEMA_VERSION


def test_load_rejects_unknown_schema_version() -> None:
    bad = {
        "schema_version": "99.0",
        "turn": 1,
        "ts_ms": 1,
        "session_id": "x",
        "budget_total": 0,
        "budget_used": 0,
        "items": [],
    }
    with pytest.raises(SchemaVersionError):
        load_manifest(json.dumps(bad))


def test_load_rejects_missing_schema_version() -> None:
    bad = {
        "turn": 1,
        "ts_ms": 1,
        "session_id": "x",
        "budget_total": 0,
        "budget_used": 0,
        "items": [],
    }
    with pytest.raises(SchemaVersionError):
        load_manifest(json.dumps(bad))


# ---------- required fields ----------

def test_load_rejects_missing_required_field() -> None:
    bad = {
        "schema_version": SCHEMA_VERSION,
        # turn is missing
        "ts_ms": 1,
        "session_id": "x",
        "budget_total": 0,
        "budget_used": 0,
        "items": [],
    }
    with pytest.raises((KeyError, TypeError, ValueError)):
        load_manifest(json.dumps(bad))


def test_item_required_fields() -> None:
    bad = {
        "schema_version": SCHEMA_VERSION,
        "turn": 1,
        "ts_ms": 1,
        "session_id": "x",
        "budget_total": 0,
        "budget_used": 0,
        "items": [
            {
                "id": "c-1",
                # bucket missing
                "source_path": "p",
                "sha256": "z" * 64,
                "token_count": 1,
                "retrieval_reason": "r",
                "last_touched_turn": 0,
                "pinned": False,
            }
        ],
    }
    with pytest.raises((KeyError, TypeError, ValueError)):
        load_manifest(json.dumps(bad))


# ---------- forward compat (x-prefixed extensions) ----------

def test_unknown_x_prefixed_keys_are_preserved() -> None:
    """Adding `x_*` fields must NOT break the loader; they round-trip
    untouched. Lets future v0.x add fields without bumping schema_version."""
    src = {
        "schema_version": SCHEMA_VERSION,
        "turn": 1,
        "ts_ms": 1,
        "session_id": "x",
        "budget_total": 0,
        "budget_used": 0,
        "items": [],
        "x_runtime_extension": {"experimental": True},
    }
    raw = json.dumps(src, sort_keys=True)
    m = load_manifest(raw)
    out = dump_manifest(m)
    parsed = json.loads(out)
    assert parsed["x_runtime_extension"] == {"experimental": True}


def test_unknown_non_x_keys_are_rejected() -> None:
    """Non-x-prefixed unknown fields are rejected: they imply intent we
    don't understand. Forces explicit schema version bumps for breaking
    additions."""
    src = {
        "schema_version": SCHEMA_VERSION,
        "turn": 1,
        "ts_ms": 1,
        "session_id": "x",
        "budget_total": 0,
        "budget_used": 0,
        "items": [],
        "future_field": "this should be rejected",
    }
    with pytest.raises(ValueError):
        load_manifest(json.dumps(src))


# ---------- structural ----------

def test_empty_items_list_is_valid() -> None:
    m = Manifest(
        schema_version=SCHEMA_VERSION,
        turn=0,
        ts_ms=0,
        session_id="empty",
        budget_total=0,
        budget_used=0,
        items=[],
    )
    out = dump_manifest(m)
    again = load_manifest(out)
    assert again.items == []


def test_budget_used_sum_invariant_documented() -> None:
    """The runtime is responsible for keeping budget_used == sum(item.token_count
    for item in items if item.bucket != 'claude_md'). The manifest type does
    NOT enforce this — it is a downstream invariant. We document it here so
    the assumption is visible."""
    m = _sample()
    raw_used = sum(it.token_count for it in m.items)
    # The sample's budget_used (18_420) deliberately doesn't match the items
    # list (only 692 tokens here) — the manifest type stores what the runtime
    # told it. Validation lives elsewhere.
    assert m.budget_used != raw_used


def test_unicode_in_source_path() -> None:
    m = Manifest(
        schema_version=SCHEMA_VERSION,
        turn=1,
        ts_ms=1,
        session_id="u",
        budget_total=100,
        budget_used=10,
        items=[
            InjectionItemSnapshot(
                id="c-u-1",
                bucket="hot",
                source_path="docs/résumé.md",
                sha256="0" * 64,
                token_count=10,
                retrieval_reason="r",
                last_touched_turn=1,
                pinned=False,
            ),
        ],
    )
    out = dump_manifest(m)
    again = load_manifest(out)
    assert again.items[0].source_path == "docs/résumé.md"
