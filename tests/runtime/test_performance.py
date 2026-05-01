"""Phase 4d: performance micro-benchmarks for the runtime hot path.

Targets the question "will runtime hooks slow down Claude Code?". Hooks
must complete fast or sessions get sluggish. We measure on synthetic
inputs and assert against generous thresholds; if these fail on a sane
laptop, the runtime needs profiling before shipping.

These tests are designed to run in <1 second total. They aren't trying
to publish numbers; they're trying to detect 10x regressions early.
"""
from __future__ import annotations

import json
import os
import sys
import time
from io import StringIO
from pathlib import Path

import pytest

from runtime.adapters.claude_code.config import RuntimeConfig
from runtime.adapters.claude_code.hooks import handle_hook
from runtime.core.budget import (
    AddItem,
    Engine,
    SessionStart,
    TurnAdvance,
)
from runtime.core.events import EVENT_LOG_SCHEMA_VERSION, EventRecord, append_event, dump_event, load_events
from runtime.core.locking import locked_append
from runtime.core.manifest import InjectionItemSnapshot, dump_manifest
from runtime.core.policy.defaults.lru import LRUPolicy
from runtime.core.replay import ReplayConfig, replay
from runtime.core.tokens import OfflineTokenCounter


# Generous thresholds (10x of expected cost on a modern laptop). These exist
# to catch O(n^2) regressions, not to be precise benchmarks.
P95_HOOK_MS = 100.0           # adapter handle_hook should be sub-100ms
P95_ENGINE_APPLY_MS = 5.0     # one engine event apply
P95_LOCKED_APPEND_MS = 50.0   # one locked append (filesystem-bound)
P95_DUMP_EVENT_MS = 5.0       # one event dump


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(len(s) * pct / 100)))
    return s[k]


def test_handle_hook_p95_under_threshold(tmp_path: Path, monkeypatch) -> None:
    """SessionStart hook should complete in well under P95_HOOK_MS."""
    cfg = RuntimeConfig(log_dir=tmp_path / "logs")
    times: list[float] = []
    for _ in range(50):
        monkeypatch.setattr(sys, "stdin", StringIO(json.dumps({"session_id": "s"})))
        t0 = time.perf_counter()
        handle_hook("SessionStart", config=cfg)
        times.append((time.perf_counter() - t0) * 1000)
    p95 = _percentile(times, 95)
    assert p95 < P95_HOOK_MS, f"handle_hook p95 = {p95:.2f}ms exceeds {P95_HOOK_MS}ms"


def test_handle_hook_post_tool_use_with_payload(tmp_path: Path, monkeypatch) -> None:
    """PostToolUse with a 5KB payload — the realistic case."""
    cfg = RuntimeConfig(log_dir=tmp_path / "logs")
    payload_text = "x " * 2500  # ~5KB
    payload = {
        "session_id": "s",
        "tool_name": "Read",
        "tool_input": {"file_path": "/tmp/foo.md"},
        "tool_response": payload_text,
    }
    times: list[float] = []
    for _ in range(50):
        monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))
        t0 = time.perf_counter()
        handle_hook("PostToolUse", config=cfg)
        times.append((time.perf_counter() - t0) * 1000)
    p95 = _percentile(times, 95)
    assert p95 < P95_HOOK_MS, f"PostToolUse p95 = {p95:.2f}ms exceeds {P95_HOOK_MS}ms"


def test_engine_apply_p95_under_threshold() -> None:
    """Engine.apply() per call must be very fast — it's called many times."""
    eng = Engine(budgets={"retrieved": 100_000}, policy=LRUPolicy(), session_id="perf")
    eng.apply(SessionStart(ts_ms=0))
    times: list[float] = []
    for i in range(500):
        t0 = time.perf_counter()
        eng.apply(AddItem(
            id=f"c-{i}", bucket="retrieved",
            source_path=f"p/{i}.md", sha256="0" * 64,
            token_count=100, retrieval_reason="r", pinned=False,
        ))
        times.append((time.perf_counter() - t0) * 1000)
    p95 = _percentile(times, 95)
    assert p95 < P95_ENGINE_APPLY_MS, f"engine.apply p95 = {p95:.2f}ms exceeds {P95_ENGINE_APPLY_MS}ms"


def test_engine_apply_with_eviction_p95() -> None:
    """When over cap, apply triggers eviction. Should still be fast."""
    eng = Engine(budgets={"retrieved": 5_000}, policy=LRUPolicy(), session_id="perf")
    eng.apply(SessionStart(ts_ms=0))
    times: list[float] = []
    for i in range(200):
        t0 = time.perf_counter()
        eng.apply(AddItem(
            id=f"c-{i}", bucket="retrieved",
            source_path=f"p/{i}.md", sha256="0" * 64,
            token_count=400, retrieval_reason="r", pinned=False,
        ))
        times.append((time.perf_counter() - t0) * 1000)
    p95 = _percentile(times, 95)
    assert p95 < P95_ENGINE_APPLY_MS * 4, (
        f"engine.apply (with eviction) p95 = {p95:.2f}ms; "
        f"target {P95_ENGINE_APPLY_MS * 4}ms"
    )


def test_locked_append_p95_under_threshold(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    times: list[float] = []
    for _ in range(50):
        t0 = time.perf_counter()
        locked_append(log, '{"event":"X","ts_ms":1}')
        times.append((time.perf_counter() - t0) * 1000)
    p95 = _percentile(times, 95)
    assert p95 < P95_LOCKED_APPEND_MS, f"locked_append p95 = {p95:.2f}ms"


def test_dump_event_p95_under_threshold() -> None:
    e = EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=1, event="PostToolUse", session_id="s", turn=0,
        tool_name="Read", tool_input_keys=["file_path", "limit"],
        items_added=[
            InjectionItemSnapshot(
                id=f"c-{i:03d}", bucket="retrieved",
                source_path=f"p/{i}.md", sha256="0" * 64,
                token_count=100, retrieval_reason="r",
                last_touched_turn=0, pinned=False, score=0.0,
            )
            for i in range(5)
        ],
    )
    times: list[float] = []
    for _ in range(200):
        t0 = time.perf_counter()
        dump_event(e)
        times.append((time.perf_counter() - t0) * 1000)
    p95 = _percentile(times, 95)
    assert p95 < P95_DUMP_EVENT_MS


def test_replay_500_events_completes_quickly(tmp_path: Path) -> None:
    """Replay of a 500-event log should be sub-second."""
    log = tmp_path / "events.log.jsonl"
    snap = lambda i: InjectionItemSnapshot(
        id=f"c-{i:04d}", bucket="retrieved",
        source_path=f"p/{i}.md", sha256="0" * 64,
        token_count=100, retrieval_reason="r",
        last_touched_turn=0, pinned=False, score=0.0,
    )
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=1, event="SessionStart", session_id="s", turn=0))
    for t in range(50):
        append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=2 + t, event="UserPromptSubmit", session_id="s", turn=t + 1))
        for k in range(9):
            append_event(log, EventRecord(
                EVENT_LOG_SCHEMA_VERSION, ts_ms=10 + t * 10 + k,
                event="PostToolUse", session_id="s", turn=t + 1,
                items_added=[snap(t * 9 + k)],
            ))

    cfg = ReplayConfig(budgets={"retrieved": 100_000}, policy=LRUPolicy(), session_id="s")
    t0 = time.perf_counter()
    summary = replay(log, cfg)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert summary.n_events >= 500
    assert elapsed_ms < 1000, f"replay of 500-event log took {elapsed_ms:.0f}ms"


def test_token_counter_throughput() -> None:
    """The offline counter is on the hot path of every PostToolUse. Make sure
    it can do MB/s on typical input."""
    counter = OfflineTokenCounter()
    text = "the quick brown fox jumps over the lazy dog " * 1000  # ~45KB
    t0 = time.perf_counter()
    for _ in range(20):
        counter.count(text)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    # 20 iterations of 45KB = ~900KB. Should be sub-second.
    assert elapsed_ms < 1000, f"token counter throughput too low: {elapsed_ms:.0f}ms for 900KB"
