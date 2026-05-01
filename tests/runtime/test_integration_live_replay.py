"""Phase 3g/h: integration test — live engine + replay byte-equal contract.

This is the proof-of-concept test for the replay substrate. Procedure:

  1. Run a session through the live Engine, simulating an adapter writing
     EventRecord rows to a log as Engine state mutates.
  2. After the session, capture the live Engine's final manifest.
  3. Replay the log through a fresh Engine.
  4. Assert the replayed final manifest equals the live final manifest.

If these two diverge, replay is dishonest. The whole "audit-from-artifacts"
story falls apart. This test pins the contract.
"""
from __future__ import annotations

from pathlib import Path

from runtime.core.budget import (
    AddItem,
    Engine,
    EvictItem,
    SessionStart,
    TouchItem,
    TurnAdvance,
)
from runtime.core.events import EVENT_LOG_SCHEMA_VERSION, EventRecord, append_event
from runtime.core.manifest import InjectionItemSnapshot, dump_manifest
from runtime.core.policy.defaults.lru import LRUPolicy
from runtime.core.replay import ReplayConfig, replay


def _snap(snap_id: str, *, bucket: str = "retrieved", tokens: int = 400, pinned: bool = False) -> InjectionItemSnapshot:
    return InjectionItemSnapshot(
        id=snap_id,
        bucket=bucket,
        source_path=f"path/{snap_id}.md",
        sha256="0" * 64,
        token_count=tokens,
        retrieval_reason="test",
        last_touched_turn=0,
        pinned=pinned,
        score=0.0,
    )


def test_live_session_replays_to_byte_equal_final_manifest(tmp_path: Path) -> None:
    """Run a session through Engine + record EventRecords; replay the
    recorded log through a fresh Engine; assert the final manifests are
    byte-identical."""
    log = tmp_path / "events.log.jsonl"
    budgets = {"hot": 1000, "retrieved": 2000, "scratchpad": 500}
    policy = LRUPolicy()
    session_id = "integration-test"

    # ---- live run ----
    live_eng = Engine(budgets=budgets, policy=policy, session_id=session_id)
    live_eng.apply(SessionStart(ts_ms=1))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=1, event="SessionStart", session_id=session_id, turn=0))

    def add_with_log(item_id: str, *, bucket: str = "retrieved", tokens: int = 400, ts_ms: int) -> None:
        # Snapshot the engine state BEFORE the add to detect what gets evicted
        before = {it.id for it in live_eng.snapshot().items}
        snap = _snap(item_id, bucket=bucket, tokens=tokens)
        live_eng.apply(AddItem(
            id=snap.id, bucket=snap.bucket, source_path=snap.source_path,
            sha256=snap.sha256, token_count=snap.token_count,
            retrieval_reason=snap.retrieval_reason, pinned=snap.pinned, score=snap.score,
        ))
        after = {it.id for it in live_eng.snapshot().items}
        evicted = sorted(before - after)
        append_event(log, EventRecord(
            EVENT_LOG_SCHEMA_VERSION, ts_ms=ts_ms, event="PostToolUse",
            session_id=session_id, turn=live_eng.current_turn,
            tool_name="Read",
            items_added=[snap],
            item_ids_evicted=evicted,
        ))

    # Simulate: 6 turns, 1-2 adds per turn, totaling enough to breach the 2000-token cap
    for t in range(1, 7):
        live_eng.apply(TurnAdvance(ts_ms=10 * t))
        append_event(log, EventRecord(
            EVENT_LOG_SCHEMA_VERSION, ts_ms=10 * t, event="UserPromptSubmit",
            session_id=session_id, turn=live_eng.current_turn,
        ))
        add_with_log(f"c-{t}-a", tokens=400, ts_ms=10 * t + 1)
        add_with_log(f"c-{t}-b", tokens=300, ts_ms=10 * t + 2)

    live_final = dump_manifest(live_eng.snapshot())

    # ---- replayed run ----
    config = ReplayConfig(budgets=budgets, policy=policy, session_id=session_id)
    summary = replay(log, config)
    replay_final = dump_manifest(summary.manifests[-1])

    assert live_final == replay_final, (
        "live and replayed manifests diverged; replay substrate is dishonest"
    )


def test_replay_handles_explicit_evict_in_log(tmp_path: Path) -> None:
    """When the live system records explicit item_ids_evicted, replay
    must apply them too (they were policy decisions made live)."""
    log = tmp_path / "events.log.jsonl"
    config = ReplayConfig(
        budgets={"hot": 1000, "retrieved": 2000},
        policy=LRUPolicy(),
        session_id="evict-test",
    )

    snap_a = _snap("a", tokens=100)
    snap_b = _snap("b", tokens=100)

    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=1, event="SessionStart", session_id="evict-test", turn=0))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=2, event="UserPromptSubmit", session_id="evict-test", turn=1))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=3, event="PostToolUse", session_id="evict-test", turn=1, items_added=[snap_a]))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=4, event="UserPromptSubmit", session_id="evict-test", turn=2))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=5, event="PostToolUse", session_id="evict-test", turn=2, items_added=[snap_b], item_ids_evicted=["a"]))

    summary = replay(log, config)
    final = summary.manifests[-1]
    ids = {it.id for it in final.items}
    assert "a" not in ids, "explicit evict in log was ignored by replay"
    assert "b" in ids


def test_replay_with_compaction_event_does_not_crash(tmp_path: Path) -> None:
    """PostCompact / Notification / Stop are noops in v0.2 replay.
    The log may contain them; replay must tolerate them without exception."""
    log = tmp_path / "events.log.jsonl"
    config = ReplayConfig(budgets={"retrieved": 1000}, policy=LRUPolicy(), session_id="x")
    snap = _snap("a")
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=1, event="SessionStart", session_id="x", turn=0))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=2, event="UserPromptSubmit", session_id="x", turn=1))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=3, event="PostToolUse", session_id="x", turn=1, items_added=[snap]))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=4, event="PostCompact", session_id="x", turn=1))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=5, event="Notification", session_id="x", turn=1))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=6, event="Stop", session_id="x", turn=1))
    summary = replay(log, config)  # must not raise
    assert summary.n_events == 6
