"""v0.3.0: re-injection composer + intent round-trip tests."""
from __future__ import annotations

import json

import pytest

from runtime.adapters.claude_code.reinjection import (
    REINJECT_CLOSE,
    REINJECT_OPEN,
    ReinjectionContext,
    build_reinjection_block,
    collect_user_intent_events,
)
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
)


def _snap(snap_id: str, *, bucket: str = "hot", tokens: int = 100, pinned: bool = False, path: str = "p.md") -> InjectionItemSnapshot:
    return InjectionItemSnapshot(
        id=snap_id,
        bucket=bucket,
        source_path=path,
        sha256="0" * 64,
        token_count=tokens,
        retrieval_reason="r",
        last_touched_turn=0,
        pinned=pinned,
        score=0.0,
    )


def _empty_manifest(items: list[InjectionItemSnapshot] | None = None) -> Manifest:
    return Manifest(
        schema_version=SCHEMA_VERSION,
        turn=0,
        ts_ms=0,
        session_id="t",
        budget_total=0,
        budget_used=0,
        items=items or [],
    )


# ---------- intent field round-trip on EventRecord ----------

def test_event_intent_roundtrip() -> None:
    e = EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=1, event="PostToolUse", session_id="s", turn=0,
        intent="user-add",
    )
    raw = dump_event(e)
    again = load_event(raw)
    assert again.intent == "user-add"


def test_event_intent_omitted_when_empty() -> None:
    """When intent is empty string, it should NOT appear in the JSON dump
    so old logs without intent stay byte-identical post-write."""
    e = EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=1, event="Stop", session_id="s", turn=0,
    )
    raw = dump_event(e)
    parsed = json.loads(raw)
    assert "intent" not in parsed


def test_event_intent_default_empty_when_missing() -> None:
    """Loader handles old logs (no intent field) by defaulting to ''."""
    raw = json.dumps({
        "schema_version": EVENT_LOG_SCHEMA_VERSION,
        "ts_ms": 1, "event": "Stop", "session_id": "s", "turn": 0,
    })
    e = load_event(raw)
    assert e.intent == ""


# ---------- composer: empty path ----------

def test_block_returns_empty_when_nothing_to_say() -> None:
    ctx = ReinjectionContext(
        manifest=_empty_manifest(),
        user_added_items=[],
        user_evicted_ids=[],
        item_content_by_id={},
    )
    assert build_reinjection_block(ctx) == ""


# ---------- composer: pinned items ----------

def test_block_includes_pinned_items() -> None:
    pinned_item = _snap("c-pin1", pinned=True, path="hot/pinned.md")
    ctx = ReinjectionContext(
        manifest=_empty_manifest([pinned_item]),
        user_added_items=[],
        user_evicted_ids=[],
        item_content_by_id={"c-pin1": "do not forget this"},
    )
    out = build_reinjection_block(ctx)
    assert REINJECT_OPEN in out
    assert REINJECT_CLOSE in out
    assert "always-relevant" in out
    assert "c-pin1" in out
    assert "do not forget this" in out
    assert "hot/pinned.md" in out


# ---------- composer: user-add items ----------

def test_block_includes_user_added_items() -> None:
    added = _snap("c-add1", path="lessons/postgres.md")
    ctx = ReinjectionContext(
        manifest=_empty_manifest(),
        user_added_items=[added],
        user_evicted_ids=[],
        item_content_by_id={"c-add1": "use SELECT FOR UPDATE SKIP LOCKED"},
    )
    out = build_reinjection_block(ctx)
    assert "User just added these for this turn:" in out
    assert "c-add1" in out
    assert "SELECT FOR UPDATE SKIP LOCKED" in out


# ---------- composer: user-evict ids ----------

def test_block_includes_user_evicted_ids() -> None:
    ctx = ReinjectionContext(
        manifest=_empty_manifest(),
        user_added_items=[],
        user_evicted_ids=["c-old1", "c-old2"],
        item_content_by_id={},
    )
    out = build_reinjection_block(ctx)
    assert "explicitly removed" in out
    assert "c-old1" in out and "c-old2" in out


# ---------- composer: budget truncation ----------

def test_block_truncates_to_token_budget() -> None:
    """Massive content should get truncated with a marker line."""
    huge = "x" * 50_000
    pinned_item = _snap("c-huge", pinned=True)
    ctx = ReinjectionContext(
        manifest=_empty_manifest([pinned_item]),
        user_added_items=[],
        user_evicted_ids=[],
        item_content_by_id={"c-huge": huge},
        budget_tokens=500,  # ~2000 chars
    )
    out = build_reinjection_block(ctx)
    assert "truncated to fit re-injection budget" in out
    assert len(out) < 50_000  # truncation actually happened


# ---------- composer: priority ordering ----------

def test_pinned_appears_before_user_adds() -> None:
    pinned = _snap("c-pinned", pinned=True, path="pinned.md")
    added = _snap("c-added", path="added.md")
    ctx = ReinjectionContext(
        manifest=_empty_manifest([pinned]),
        user_added_items=[added],
        user_evicted_ids=[],
        item_content_by_id={"c-pinned": "P", "c-added": "A"},
    )
    out = build_reinjection_block(ctx)
    assert out.index("c-pinned") < out.index("c-added")


# ---------- collect_user_intent_events helper ----------

def test_collect_filters_by_intent() -> None:
    snap_added = _snap("c-add")
    events = [
        EventRecord(
            schema_version=EVENT_LOG_SCHEMA_VERSION, ts_ms=1, event="PostToolUse",
            session_id="s", turn=0, items_added=[_snap("c-other")],  # NOT user-add
        ),
        EventRecord(
            schema_version=EVENT_LOG_SCHEMA_VERSION, ts_ms=2, event="PostToolUse",
            session_id="s", turn=0, items_added=[snap_added], intent="user-add",
        ),
        EventRecord(
            schema_version=EVENT_LOG_SCHEMA_VERSION, ts_ms=3, event="PostToolUse",
            session_id="s", turn=0, item_ids_evicted=["c-evict"], intent="user-evict",
        ),
    ]
    added, evicted = collect_user_intent_events(events)
    assert [it.id for it in added] == ["c-add"]
    assert evicted == ["c-evict"]


def test_collect_respects_since_ts_ms() -> None:
    """Filter to events after a given timestamp (used for 'since last UserPromptSubmit')."""
    events = [
        EventRecord(
            schema_version=EVENT_LOG_SCHEMA_VERSION, ts_ms=10, event="PostToolUse",
            session_id="s", turn=0, items_added=[_snap("c-old")], intent="user-add",
        ),
        EventRecord(
            schema_version=EVENT_LOG_SCHEMA_VERSION, ts_ms=100, event="PostToolUse",
            session_id="s", turn=0, items_added=[_snap("c-new")], intent="user-add",
        ),
    ]
    added, _ = collect_user_intent_events(events, since_ts_ms=50)
    assert [it.id for it in added] == ["c-new"]
