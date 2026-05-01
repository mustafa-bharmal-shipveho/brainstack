"""Phase 3d: budget enforcer (Engine) tests.

The Engine is what makes this a runtime, not a logger. Contract:

  - Receives a stream of additive ToolEvents (a Read happened, a Grep happened).
  - Maintains internal state: which items are "currently injected", per bucket.
  - Enforces budgets per bucket. When over cap, calls Policy.choose_evictions
    and demotes those items.
  - Produces a Manifest snapshot per turn.
  - Pure function over input events: same events in -> same manifest out.

The control property (steel-man "fancy logger" refutation):
  "An item evicted at turn N MUST NOT appear in the manifest at turn N+1
  unless an explicit add event for that item arrives at turn N+1."
"""
from __future__ import annotations

import pytest

from runtime.core.budget import (
    AddItem,
    Engine,
    EvictItem,
    SessionStart,
    TouchItem,
    TurnAdvance,
)
from runtime.core.policy.defaults.lru import LRUPolicy


@pytest.fixture
def engine() -> Engine:
    return Engine(
        budgets={"hot": 1000, "retrieved": 2000, "scratchpad": 500},
        policy=LRUPolicy(),
        session_id="test-session",
    )


def _add(eng: Engine, item_id: str, bucket: str = "retrieved", tokens: int = 100, *, pinned: bool = False) -> None:
    eng.apply(AddItem(
        id=item_id,
        bucket=bucket,
        source_path=f"path/{item_id}.md",
        sha256="0" * 64,
        token_count=tokens,
        retrieval_reason="test",
        pinned=pinned,
    ))


# ---------- session lifecycle ----------

def test_engine_starts_empty(engine: Engine) -> None:
    m = engine.snapshot()
    assert m.items == []
    assert m.budget_used == 0


def test_session_start_records_turn(engine: Engine) -> None:
    engine.apply(SessionStart(ts_ms=1))
    assert engine.current_turn == 0


def test_turn_advance_increments(engine: Engine) -> None:
    engine.apply(SessionStart(ts_ms=1))
    engine.apply(TurnAdvance(ts_ms=2))
    assert engine.current_turn == 1
    engine.apply(TurnAdvance(ts_ms=3))
    assert engine.current_turn == 2


# ---------- adding items ----------

def test_add_item_updates_manifest(engine: Engine) -> None:
    engine.apply(SessionStart(ts_ms=1))
    _add(engine, "c-1", tokens=200)
    m = engine.snapshot()
    assert len(m.items) == 1
    assert m.items[0].id == "c-1"
    assert m.items[0].token_count == 200


def test_add_item_increments_budget(engine: Engine) -> None:
    engine.apply(SessionStart(ts_ms=1))
    _add(engine, "c-1", tokens=200)
    _add(engine, "c-2", tokens=300)
    m = engine.snapshot()
    # budget_used is the sum of token_counts in non-claude_md buckets
    assert m.budget_used == 500


def test_re_adding_same_id_replaces_in_place(engine: Engine) -> None:
    engine.apply(SessionStart(ts_ms=1))
    _add(engine, "c-1", tokens=100)
    _add(engine, "c-1", tokens=999)  # update with new token count
    m = engine.snapshot()
    assert len(m.items) == 1
    assert m.items[0].token_count == 999


# ---------- touch ----------

def test_touch_updates_last_touched(engine: Engine) -> None:
    engine.apply(SessionStart(ts_ms=1))
    _add(engine, "c-1", tokens=100)
    engine.apply(TurnAdvance(ts_ms=2))
    engine.apply(TurnAdvance(ts_ms=3))
    engine.apply(TouchItem(id="c-1", ts_ms=4))
    m = engine.snapshot()
    assert m.items[0].last_touched_turn == 2  # turn after 2 advances


def test_touch_unknown_item_is_noop(engine: Engine) -> None:
    engine.apply(SessionStart(ts_ms=1))
    engine.apply(TouchItem(id="never-added", ts_ms=2))  # should not raise
    assert engine.snapshot().items == []


# ---------- budget enforcement (the headline test) ----------

def test_over_budget_triggers_eviction(engine: Engine) -> None:
    """Add items until 'retrieved' bucket exceeds its 2000-token cap.
    The engine must run the policy and demote items."""
    engine.apply(SessionStart(ts_ms=1))
    _add(engine, "c-1", bucket="retrieved", tokens=500)
    engine.apply(TurnAdvance(ts_ms=2))
    _add(engine, "c-2", bucket="retrieved", tokens=500)
    engine.apply(TurnAdvance(ts_ms=3))
    _add(engine, "c-3", bucket="retrieved", tokens=500)
    engine.apply(TurnAdvance(ts_ms=4))
    _add(engine, "c-4", bucket="retrieved", tokens=500)
    engine.apply(TurnAdvance(ts_ms=5))
    _add(engine, "c-5", bucket="retrieved", tokens=500)  # 2500 > cap=2000

    m = engine.snapshot()
    total = sum(it.token_count for it in m.items if it.bucket == "retrieved")
    assert total <= 2000, f"bucket exceeded cap: {total}"


def test_evicted_item_does_not_reappear_without_explicit_readd(engine: Engine) -> None:
    """THE control property. This is the test that refutes the Skeptic's
    'fancy logger' critique. After eviction, an item is GONE from the
    manifest. Only an explicit AddItem brings it back."""
    engine.apply(SessionStart(ts_ms=1))
    # Fill above cap to force eviction
    for i in range(5):
        engine.apply(TurnAdvance(ts_ms=i + 2))
        _add(engine, f"c-{i}", bucket="retrieved", tokens=500)

    m1 = engine.snapshot()
    evicted_ids = {f"c-{i}" for i in range(5)} - {it.id for it in m1.items}
    assert len(evicted_ids) > 0, "expected at least one eviction; budget was breached"

    # Now do MANY more turns with no new adds. Evicted items must not
    # reappear. This is the property an honest runtime must guarantee.
    for t in range(10):
        engine.apply(TurnAdvance(ts_ms=100 + t))

    m2 = engine.snapshot()
    seen_ids_after = {it.id for it in m2.items}
    for ev_id in evicted_ids:
        assert ev_id not in seen_ids_after, (
            f"evicted item {ev_id} reappeared at turn {engine.current_turn}; "
            f"runtime is acting like a logger, not a controller"
        )


def test_explicit_readd_after_eviction_works(engine: Engine) -> None:
    """The flip side: after eviction, an explicit AddItem with the same
    id must succeed. This is also part of the contract."""
    engine.apply(SessionStart(ts_ms=1))
    for i in range(5):
        engine.apply(TurnAdvance(ts_ms=i + 2))
        _add(engine, f"c-{i}", bucket="retrieved", tokens=500)

    m1 = engine.snapshot()
    evicted = next(iter({f"c-{i}" for i in range(5)} - {it.id for it in m1.items}))

    # Re-add it explicitly
    engine.apply(TurnAdvance(ts_ms=200))
    _add(engine, evicted, bucket="retrieved", tokens=100)

    m2 = engine.snapshot()
    assert evicted in {it.id for it in m2.items}


def test_pinned_items_survive_eviction(engine: Engine) -> None:
    engine.apply(SessionStart(ts_ms=1))
    _add(engine, "p-1", bucket="retrieved", tokens=500, pinned=True)
    for i in range(5):
        engine.apply(TurnAdvance(ts_ms=i + 2))
        _add(engine, f"c-{i}", bucket="retrieved", tokens=500)

    m = engine.snapshot()
    ids = {it.id for it in m.items}
    assert "p-1" in ids, "pinned item was evicted"


# ---------- determinism ----------

def test_same_event_stream_produces_same_manifest() -> None:
    """Replay determinism contract."""
    def run() -> str:
        eng = Engine(
            budgets={"hot": 1000, "retrieved": 2000},
            policy=LRUPolicy(),
            session_id="det",
        )
        eng.apply(SessionStart(ts_ms=1))
        for i in range(8):
            eng.apply(TurnAdvance(ts_ms=2 + i))
            eng.apply(AddItem(
                id=f"c-{i}", bucket="retrieved",
                source_path=f"p/{i}.md", sha256="0" * 64,
                token_count=400, retrieval_reason="test", pinned=False,
            ))
        from runtime.core.manifest import dump_manifest
        return dump_manifest(eng.snapshot())

    assert run() == run()


def test_manifest_snapshot_has_correct_session_id(engine: Engine) -> None:
    engine.apply(SessionStart(ts_ms=1))
    _add(engine, "c-1")
    assert engine.snapshot().session_id == "test-session"


def test_manifest_snapshot_records_current_turn(engine: Engine) -> None:
    engine.apply(SessionStart(ts_ms=1))
    engine.apply(TurnAdvance(ts_ms=2))
    engine.apply(TurnAdvance(ts_ms=3))
    _add(engine, "c-1")
    m = engine.snapshot()
    assert m.turn == 2  # 2 advances after start


# ---------- explicit evict ----------

def test_explicit_evict_removes_item(engine: Engine) -> None:
    engine.apply(SessionStart(ts_ms=1))
    _add(engine, "c-1")
    engine.apply(EvictItem(id="c-1", reason="user-requested"))
    assert engine.snapshot().items == []


def test_explicit_evict_unknown_is_noop(engine: Engine) -> None:
    engine.apply(SessionStart(ts_ms=1))
    engine.apply(EvictItem(id="never-existed", reason="test"))  # no raise
    assert engine.snapshot().items == []
