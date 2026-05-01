"""The Engine — the runtime's state machine and budget enforcer.

This is what makes brainstack-runtime a runtime, not a logger. The Engine:

  - Receives a stream of events (SessionStart, TurnAdvance, AddItem,
    TouchItem, EvictItem).
  - Maintains internal state: which items are currently injected, per bucket.
  - Enforces budgets per bucket. When a bucket exceeds its cap, calls
    Policy.choose_evictions() and demotes the chosen items.
  - Produces a Manifest snapshot on demand.

Pure function over input events. Same events in -> same manifest out. This
property is what makes replay (Phase 3f) honest: replay constructs an Engine,
feeds it the recorded event log, and snapshots after each turn. The result
must match the original session's manifest byte-for-byte.

The control property (Skeptic finding #1, refuted here):
  An item evicted at turn N does NOT appear in the manifest at turn N+1
  unless an explicit AddItem(id) arrives at turn N+1.

This is the difference between a runtime and a logger. A logger records
what happened; a runtime decides what happens next.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from runtime.core.manifest import (
    InjectionItemSnapshot,
    Manifest,
    SCHEMA_VERSION,
)
from runtime.core.policy import EvictionRequest, InjectionItem, Policy


# ---------- event types fed into the engine ----------

@dataclass(frozen=True)
class SessionStart:
    ts_ms: int


@dataclass(frozen=True)
class TurnAdvance:
    ts_ms: int


@dataclass(frozen=True)
class AddItem:
    id: str
    bucket: str
    source_path: str
    sha256: str
    token_count: int
    retrieval_reason: str
    pinned: bool = False
    score: float = 0.0


@dataclass(frozen=True)
class TouchItem:
    id: str
    ts_ms: int


@dataclass(frozen=True)
class EvictItem:
    id: str
    reason: str


@dataclass(frozen=True)
class PinItem:
    """Marks an existing item as pinned. No-op if the id isn't in state."""
    id: str


@dataclass(frozen=True)
class UnpinItem:
    """Removes the pinned mark from an item. No-op if not pinned or absent."""
    id: str


# ---------- the engine ----------

class Engine:
    """State machine over the event stream.

    Buckets that count against the budget: anything that's not "claude_md".
    The CLAUDE.md content is sized at file-load time and is not subject to
    in-session eviction (the user controls it). Other buckets — hot,
    retrieved, scratchpad — are budget-managed.
    """

    def __init__(
        self,
        *,
        budgets: dict[str, int],
        policy: Policy,
        session_id: str,
        ts_ms: int = 0,
    ):
        self.budgets = dict(budgets)
        self.policy = policy
        self.session_id = session_id
        self._items: dict[str, InjectionItem] = {}
        self._turn = 0
        self._ts_ms = ts_ms

    # --- public API ---

    @property
    def current_turn(self) -> int:
        return self._turn

    def apply(self, event: object) -> None:
        """Single dispatch on event type."""
        if isinstance(event, SessionStart):
            self._on_session_start(event)
        elif isinstance(event, TurnAdvance):
            self._on_turn_advance(event)
        elif isinstance(event, AddItem):
            self._on_add(event)
        elif isinstance(event, TouchItem):
            self._on_touch(event)
        elif isinstance(event, EvictItem):
            self._on_evict(event)
        elif isinstance(event, PinItem):
            self._on_pin(event)
        elif isinstance(event, UnpinItem):
            self._on_unpin(event)
        else:
            raise TypeError(f"unknown event type: {type(event).__name__}")

    def apply_all(self, events: Iterable[object]) -> None:
        for e in events:
            self.apply(e)

    def snapshot(self) -> Manifest:
        """Build the current manifest snapshot."""
        items = [
            InjectionItemSnapshot(
                id=it.id,
                bucket=it.bucket,
                source_path=it.source_path,
                sha256=it.sha256,
                token_count=it.token_count,
                retrieval_reason=it.retrieval_reason,
                last_touched_turn=it.last_touched_turn,
                pinned=it.pinned,
                score=it.score,
                extensions=it.extensions if isinstance(it.extensions, dict) else {},
            )
            for it in sorted(self._items.values(), key=lambda x: x.id)
        ]
        budget_used = sum(
            it.token_count for it in self._items.values()
            if it.bucket != "claude_md"
        )
        budget_total = sum(self.budgets.values())
        return Manifest(
            schema_version=SCHEMA_VERSION,
            turn=self._turn,
            ts_ms=self._ts_ms,
            session_id=self.session_id,
            budget_total=budget_total,
            budget_used=budget_used,
            items=items,
        )

    # --- handlers ---

    def _on_session_start(self, e: SessionStart) -> None:
        self._ts_ms = e.ts_ms
        self._turn = 0

    def _on_turn_advance(self, e: TurnAdvance) -> None:
        self._ts_ms = e.ts_ms
        self._turn += 1

    def _on_add(self, e: AddItem) -> None:
        # InjectionItem doesn't have a source_path / sha256 / retrieval_reason
        # field by design (those live in the snapshot). We extend by
        # subclass-by-attribute: store the extras as instance attributes
        # via a tiny wrapper. For policy, only id/bucket/token_count/
        # last_touched_turn/pinned/score matter.
        item = _RuntimeItem(
            id=e.id,
            bucket=e.bucket,
            token_count=e.token_count,
            last_touched_turn=self._turn,
            pinned=e.pinned,
            score=e.score,
            source_path=e.source_path,
            sha256=e.sha256,
            retrieval_reason=e.retrieval_reason,
        )
        self._items[e.id] = item
        self._enforce_budget(e.bucket)

    def _on_touch(self, e: TouchItem) -> None:
        if e.id not in self._items:
            return  # noop on unknown
        old = self._items[e.id]
        self._items[e.id] = _replace(old, last_touched_turn=self._turn)

    def _on_evict(self, e: EvictItem) -> None:
        self._items.pop(e.id, None)

    def _on_pin(self, e: PinItem) -> None:
        if e.id not in self._items:
            return
        self._items[e.id] = _replace(self._items[e.id], pinned=True)

    def _on_unpin(self, e: UnpinItem) -> None:
        if e.id not in self._items:
            return
        self._items[e.id] = _replace(self._items[e.id], pinned=False)

    # --- enforcement ---

    def _enforce_budget(self, bucket: str) -> None:
        cap = self.budgets.get(bucket)
        if cap is None:
            return  # bucket has no cap; no enforcement
        in_bucket = [it for it in self._items.values() if it.bucket == bucket]
        used = sum(it.token_count for it in in_bucket)
        overflow = used - cap
        if overflow <= 0:
            return
        request = EvictionRequest(
            items=list(self._items.values()),
            current_turn=self._turn,
            evict_tokens=overflow,
            bucket=bucket,
        )
        evicted_ids = self.policy.choose_evictions(request)
        for eid in evicted_ids:
            self._items.pop(eid, None)


# Tiny helper class with all the fields a snapshot needs. We avoid touching
# InjectionItem (which is the policy-input type and intentionally minimal)
# by carrying the snapshot fields here.
@dataclass
class _RuntimeItem:
    id: str
    bucket: str
    token_count: int
    last_touched_turn: int
    pinned: bool
    score: float
    source_path: str
    sha256: str
    retrieval_reason: str
    extensions: dict = field(default_factory=dict)


def _replace(item: _RuntimeItem, **kwargs) -> _RuntimeItem:
    """Mutating-replace shim. _RuntimeItem is mutable; this exists for
    readability of the call sites."""
    new = _RuntimeItem(
        id=item.id,
        bucket=item.bucket,
        token_count=item.token_count,
        last_touched_turn=item.last_touched_turn,
        pinned=item.pinned,
        score=item.score,
        source_path=item.source_path,
        sha256=item.sha256,
        retrieval_reason=item.retrieval_reason,
        extensions=dict(item.extensions),
    )
    for k, v in kwargs.items():
        setattr(new, k, v)
    return new


__all__ = [
    "AddItem",
    "Engine",
    "EvictItem",
    "PinItem",
    "SessionStart",
    "TouchItem",
    "TurnAdvance",
    "UnpinItem",
]
