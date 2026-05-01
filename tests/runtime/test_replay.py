"""Phase 3f: replay engine tests.

Replay reads an events.log.jsonl, plays it through Engine, and emits a
per-turn manifest stream. Determinism contract: replay of the same log
must produce byte-identical manifests across runs and machines.

The diff feature surfaces what entered and left the injection set between
two turns — the basis of the "why didn't the model know X?" demo.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from runtime.core.budget import (
    AddItem,
    Engine,
    EvictItem,
    SessionStart,
    TouchItem,
    TurnAdvance,
)
from runtime.core.events import EVENT_LOG_SCHEMA_VERSION, EventRecord, append_event
from runtime.core.manifest import InjectionItemSnapshot
from runtime.core.policy.defaults.lru import LRUPolicy
from runtime.core.replay import (
    ReplayConfig,
    diff_manifests,
    iter_engine_steps,
    render_diff,
    replay,
    replay_to_manifests,
)


def _write_session(log_path: Path) -> None:
    """Write a small synthetic session to a JSONL log."""
    snap = lambda i, b="retrieved", t=400, p=False: InjectionItemSnapshot(
        id=f"c-{i}",
        bucket=b,
        source_path=f"path/{i}.md",
        sha256="0" * 64,
        token_count=t,
        retrieval_reason="test",
        last_touched_turn=0,
        pinned=p,
        score=0.0,
    )
    events = [
        EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=1, event="SessionStart", session_id="s", turn=0),
        EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=2, event="UserPromptSubmit", session_id="s", turn=1),
        EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=3, event="PostToolUse", session_id="s", turn=1, tool_name="Read", items_added=[snap(0)]),
        EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=4, event="PostToolUse", session_id="s", turn=1, tool_name="Read", items_added=[snap(1)]),
        EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=5, event="UserPromptSubmit", session_id="s", turn=2),
        EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=6, event="PostToolUse", session_id="s", turn=2, tool_name="Read", items_added=[snap(2)]),
    ]
    for e in events:
        append_event(log_path, e)


@pytest.fixture
def session_log(tmp_path: Path) -> Path:
    log = tmp_path / "events.log.jsonl"
    _write_session(log)
    return log


@pytest.fixture
def config() -> ReplayConfig:
    return ReplayConfig(
        budgets={"hot": 1000, "retrieved": 2000, "scratchpad": 500},
        policy=LRUPolicy(),
        session_id="s",
    )


# ---------- replay correctness ----------

def test_replay_reconstructs_manifests(session_log: Path, config: ReplayConfig) -> None:
    manifests = replay_to_manifests(session_log, config)
    assert len(manifests) >= 1
    final = manifests[-1]
    item_ids = {it.id for it in final.items}
    assert item_ids == {"c-0", "c-1", "c-2"}


def test_replay_is_deterministic(session_log: Path, config: ReplayConfig) -> None:
    a = replay_to_manifests(session_log, config)
    b = replay_to_manifests(session_log, config)
    from runtime.core.manifest import dump_manifest
    assert [dump_manifest(m) for m in a] == [dump_manifest(m) for m in b]


def test_replay_emits_one_manifest_per_turn(session_log: Path, config: ReplayConfig) -> None:
    manifests = replay_to_manifests(session_log, config)
    turns = [m.turn for m in manifests]
    # Should include turn 0 (start), 1, 2 — but de-duplicated to last per turn
    assert turns == sorted(turns)
    assert turns[-1] == 2


# ---------- diff ----------

def test_diff_two_manifests_added_and_removed(config: ReplayConfig) -> None:
    eng = Engine(budgets=config.budgets, policy=config.policy, session_id="s")
    eng.apply(SessionStart(ts_ms=1))
    eng.apply(AddItem(id="a", bucket="retrieved", source_path="a", sha256="0" * 64, token_count=100, retrieval_reason="r"))
    eng.apply(AddItem(id="b", bucket="retrieved", source_path="b", sha256="0" * 64, token_count=100, retrieval_reason="r"))
    m1 = eng.snapshot()

    eng.apply(TurnAdvance(ts_ms=2))
    eng.apply(EvictItem(id="a", reason="manual"))
    eng.apply(AddItem(id="c", bucket="retrieved", source_path="c", sha256="0" * 64, token_count=100, retrieval_reason="r"))
    m2 = eng.snapshot()

    d = diff_manifests(m1, m2)
    assert {it.id for it in d.added} == {"c"}
    assert {it.id for it in d.removed} == {"a"}
    assert {it.id for it in d.unchanged} == {"b"}


def test_diff_render_includes_evicted_marker(config: ReplayConfig) -> None:
    eng = Engine(budgets=config.budgets, policy=config.policy, session_id="s")
    eng.apply(SessionStart(ts_ms=1))
    eng.apply(AddItem(id="kept", bucket="retrieved", source_path="p", sha256="0" * 64, token_count=100, retrieval_reason="r"))
    m1 = eng.snapshot()
    eng.apply(TurnAdvance(ts_ms=2))
    eng.apply(EvictItem(id="kept", reason="manual"))
    eng.apply(AddItem(id="newer", bucket="retrieved", source_path="p", sha256="0" * 64, token_count=100, retrieval_reason="r"))
    m2 = eng.snapshot()
    out = render_diff(m1, m2)
    assert "kept" in out
    assert "newer" in out
    assert "evicted" in out.lower() or "removed" in out.lower() or "✗" in out or "-" in out


# ---------- replay() top-level ----------

def test_replay_top_level_returns_summary(session_log: Path, config: ReplayConfig) -> None:
    summary = replay(session_log, config)
    assert summary.n_events > 0
    assert summary.n_turns >= 1
    assert summary.session_id == "s"
    # The manifests list is exposed on the summary
    assert len(summary.manifests) == summary.n_turns


# ---------- empty / edge ----------

def test_replay_empty_log_produces_empty_manifest_list(tmp_path: Path, config: ReplayConfig) -> None:
    log = tmp_path / "empty.jsonl"
    log.write_text("")
    summary = replay(log, config)
    assert summary.n_events == 0


def test_replay_handles_blank_lines_in_log(tmp_path: Path, config: ReplayConfig) -> None:
    log = tmp_path / "events.log.jsonl"
    _write_session(log)
    # Append blank lines and re-write
    content = log.read_text()
    log.write_text("\n\n" + content + "\n\n")
    manifests = replay_to_manifests(log, config)
    assert len(manifests) >= 1


def test_iter_engine_steps_yields_one_per_event(session_log: Path, config: ReplayConfig) -> None:
    from runtime.core.events import load_events

    events = load_events(session_log)
    steps = list(iter_engine_steps(events, config))
    assert len(steps) == len(events)


def test_iter_engine_steps_captures_added_ids(session_log: Path, config: ReplayConfig) -> None:
    from runtime.core.events import load_events

    events = load_events(session_log)
    steps = list(iter_engine_steps(events, config))
    # Every PostToolUse step in the fixture adds one item — check the captured IDs match.
    post_tool_steps = [s for s in steps if s.event.event == "PostToolUse"]
    assert post_tool_steps
    for s in post_tool_steps:
        assert s.added_ids, f"expected at least one added id at PostToolUse step, got {s.added_ids}"


def test_iter_engine_steps_captures_evictions_under_tight_budget(tmp_path: Path) -> None:
    """When budget is tight, an Add forces an Evict in the same Engine.apply
    call. iter_engine_steps must surface those eviction ids."""
    from runtime.core.events import EVENT_LOG_SCHEMA_VERSION, EventRecord, append_event
    from runtime.core.manifest import InjectionItemSnapshot
    from runtime.core.policy.defaults.lru import LRUPolicy

    log = tmp_path / "events.log.jsonl"
    snap = lambda i, t: InjectionItemSnapshot(
        id=f"c-{i}", bucket="retrieved", source_path=f"p/{i}.md",
        sha256="0" * 64, token_count=t, retrieval_reason="r",
        last_touched_turn=0, pinned=False, score=0.0,
    )
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=1, event="SessionStart", session_id="s", turn=0))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=2, event="UserPromptSubmit", session_id="s", turn=1))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=3, event="PostToolUse", session_id="s", turn=1, items_added=[snap(0, 400)]))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=4, event="PostToolUse", session_id="s", turn=1, items_added=[snap(1, 400)]))
    # This third add (700 tok) blows the 800-tok cap and forces eviction.
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=5, event="PostToolUse", session_id="s", turn=1, items_added=[snap(2, 700)]))

    cfg = ReplayConfig(budgets={"retrieved": 800}, policy=LRUPolicy(), session_id="s")
    from runtime.core.events import load_events
    events = load_events(log)
    steps = list(iter_engine_steps(events, cfg))
    # The third PostToolUse (last step) should show evictions.
    last = steps[-1]
    assert last.event.event == "PostToolUse"
    assert last.evicted_ids, "expected evictions when adding 700 tok over an 800 tok cap"


def test_iter_engine_steps_works_on_single_turn_session(tmp_path: Path) -> None:
    """The dogfood-revealed case: one turn, many tool calls. iter_engine_steps
    must produce one step per event without complaint."""
    from runtime.core.events import EVENT_LOG_SCHEMA_VERSION, EventRecord, append_event
    from runtime.core.manifest import InjectionItemSnapshot
    from runtime.core.policy.defaults.lru import LRUPolicy

    log = tmp_path / "events.log.jsonl"
    snap = lambda i: InjectionItemSnapshot(
        id=f"c-{i:03d}", bucket="retrieved", source_path=f"p/{i}.md",
        sha256="0" * 64, token_count=100, retrieval_reason="r",
        last_touched_turn=0, pinned=False, score=0.0,
    )
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=1, event="SessionStart", session_id="s", turn=0))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=2, event="UserPromptSubmit", session_id="s", turn=1))
    for i in range(33):  # the user's actual count
        append_event(log, EventRecord(
            EVENT_LOG_SCHEMA_VERSION, ts_ms=10 + i,
            event="PostToolUse", session_id="s", turn=1,
            items_added=[snap(i)],
        ))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=100, event="Stop", session_id="s", turn=1))

    cfg = ReplayConfig(
        budgets={"retrieved": 20000},
        policy=LRUPolicy(),
        session_id="s",
    )
    from runtime.core.events import load_events
    events = load_events(log)
    steps = list(iter_engine_steps(events, cfg))
    assert len(steps) == 1 + 1 + 33 + 1  # SessionStart + UserPromptSubmit + 33 PostToolUse + Stop
    # All 33 items should be in the final manifest (under cap, no evictions)
    final = steps[-1].manifest
    assert len(final.items) == 33


def test_replay_translates_events_to_engine_actions(tmp_path: Path, config: ReplayConfig) -> None:
    """Spot-check that EventRecord.event values map sensibly:
    SessionStart -> SessionStart event
    UserPromptSubmit -> TurnAdvance
    PostToolUse with items_added -> AddItem(s)"""
    log = tmp_path / "events.log.jsonl"
    snap = InjectionItemSnapshot(
        id="x", bucket="retrieved", source_path="p",
        sha256="0" * 64, token_count=100, retrieval_reason="r",
        last_touched_turn=0, pinned=False, score=0.0,
    )
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=1, event="SessionStart", session_id="s", turn=0))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=2, event="UserPromptSubmit", session_id="s", turn=1))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=3, event="PostToolUse", session_id="s", turn=1, items_added=[snap]))

    summary = replay(log, config)
    assert summary.n_turns == 2  # turns 0 and 1
    assert any(any(it.id == "x" for it in m.items) for m in summary.manifests)
