"""Replay engine — the headline demo feature.

Reads an events.log.jsonl, plays each EventRecord through the Engine, and
emits a per-turn manifest stream. The diff feature shows what entered and
left the injection set between any two turns — the basis of the "why
didn't the model know X?" debug experience.

Determinism contract: replay of the same log produces byte-identical
manifests across runs, machines, and Python versions (within reason).
This is what makes "why didn't the model know X?" answerable from
artifacts, not vibes.

Translation EventRecord -> Engine events:
  SessionStart        -> Engine.SessionStart
  UserPromptSubmit    -> Engine.TurnAdvance
  PostToolUse         -> Engine.AddItem (one per items_added entry)
                       + Engine.EvictItem (one per item_ids_evicted)
  Stop, SubagentStop  -> noop (lifecycle markers; the next TurnAdvance
                          handles state)
  PostCompact         -> noop in v0.2 (compaction handling is roadmap)
  Other / unknown     -> noop

TouchItem is NOT emitted by replay today. last_touched_turn is set when
AddItem fires via Engine._on_add. Explicit touch events are reserved for
v0.x when adapters can record cross-tool reference chains.

The replay is intentionally tolerant of unknown event names: future
adapters may emit events the runtime didn't anticipate. Tolerant replay
is more honest than fragile replay.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from runtime.core.budget import (
    AddItem,
    Engine,
    EvictItem,
    SessionStart,
    TurnAdvance,
)
from runtime.core.events import EventRecord, load_events
from runtime.core.manifest import InjectionItemSnapshot, Manifest
from runtime.core.policy import Policy


@dataclass
class ReplayConfig:
    """What the replay engine needs to construct the same Engine the live
    session ran with. In practice this is loaded from the user's
    pyproject.toml [tool.recall.runtime] section."""
    budgets: dict[str, int]
    policy: Policy
    session_id: str


@dataclass
class ManifestDiff:
    """Set-difference between two manifests."""
    added: list[InjectionItemSnapshot] = field(default_factory=list)
    removed: list[InjectionItemSnapshot] = field(default_factory=list)
    unchanged: list[InjectionItemSnapshot] = field(default_factory=list)


@dataclass
class ReplaySummary:
    """High-level result of a replay."""
    n_events: int
    n_turns: int
    session_id: str
    manifests: list[Manifest]


# ---------- public API ----------

def replay(log_path: Path | str, config: ReplayConfig) -> ReplaySummary:
    """Replay a full session log. Returns a summary including per-turn manifests."""
    events = load_events(Path(log_path))
    manifests = _replay_to_manifests(events, config)
    return ReplaySummary(
        n_events=len(events),
        n_turns=len(manifests),
        session_id=config.session_id,
        manifests=manifests,
    )


def replay_to_manifests(log_path: Path | str, config: ReplayConfig) -> list[Manifest]:
    """Convenience wrapper returning just the per-turn manifests."""
    return replay(log_path, config).manifests


def diff_manifests(a: Manifest, b: Manifest) -> ManifestDiff:
    """Set-diff between two manifests. Items are matched by `id`."""
    a_by_id = {it.id: it for it in a.items}
    b_by_id = {it.id: it for it in b.items}
    only_a = set(a_by_id) - set(b_by_id)
    only_b = set(b_by_id) - set(a_by_id)
    both = set(a_by_id) & set(b_by_id)
    return ManifestDiff(
        added=sorted([b_by_id[i] for i in only_b], key=lambda x: x.id),
        removed=sorted([a_by_id[i] for i in only_a], key=lambda x: x.id),
        unchanged=sorted([b_by_id[i] for i in both], key=lambda x: x.id),
    )


def render_diff(a: Manifest, b: Manifest) -> str:
    """Human-readable diff. CLI consumers print this directly."""
    d = diff_manifests(a, b)
    lines: list[str] = []
    lines.append(f"turn {a.turn} -> turn {b.turn}")
    lines.append("")
    if d.removed:
        lines.append(f"evicted ({len(d.removed)}):")
        for it in d.removed:
            lines.append(
                f"  - {it.id:<10} ({it.bucket:<10} {it.token_count:>5} tok) {it.source_path}"
            )
    if d.added:
        lines.append(f"added ({len(d.added)}):")
        for it in d.added:
            lines.append(
                f"  + {it.id:<10} ({it.bucket:<10} {it.token_count:>5} tok) {it.source_path}"
            )
    if d.unchanged:
        lines.append(f"unchanged: {len(d.unchanged)} items")
    if not (d.added or d.removed):
        lines.append("(no items added or removed)")
    return "\n".join(lines) + "\n"


# ---------- internals ----------

def _replay_to_manifests(events: list[EventRecord], config: ReplayConfig) -> list[Manifest]:
    """Drive the engine through the event stream; capture a manifest per turn."""
    eng = Engine(
        budgets=config.budgets,
        policy=config.policy,
        session_id=config.session_id,
    )
    manifests: list[Manifest] = []
    last_seen_turn = -1
    for ev in events:
        for engine_event in _translate(ev):
            eng.apply(engine_event)
        # Capture a manifest snapshot when we leave a turn boundary
        if eng.current_turn != last_seen_turn:
            if manifests:
                # We've moved to a new turn; the last snapshot is now finalized.
                pass  # nothing to do; we always re-snapshot below
            last_seen_turn = eng.current_turn
        # Always update the "current" manifest at the back; we'll dedupe
        # to one-per-turn at the end
        snap = eng.snapshot()
        if manifests and manifests[-1].turn == snap.turn:
            manifests[-1] = snap
        else:
            manifests.append(snap)
    return manifests


def _translate(ev: EventRecord) -> Iterable[object]:
    """Map an EventRecord to a sequence of Engine events."""
    if ev.event == "SessionStart":
        yield SessionStart(ts_ms=ev.ts_ms)
        return
    if ev.event in {"UserPromptSubmit"}:
        yield TurnAdvance(ts_ms=ev.ts_ms)
        return
    if ev.event == "PostToolUse":
        for snap in ev.items_added:
            if not isinstance(snap, InjectionItemSnapshot):
                # Tolerate dicts that didn't get reified (older or
                # forward-compat events); skip them for engine purposes
                continue
            yield AddItem(
                id=snap.id,
                bucket=snap.bucket,
                source_path=snap.source_path,
                sha256=snap.sha256,
                token_count=snap.token_count,
                retrieval_reason=snap.retrieval_reason,
                pinned=snap.pinned,
                score=snap.score,
            )
        for eid in ev.item_ids_evicted:
            yield EvictItem(id=eid, reason="adapter-recorded")
        return
    # Stop, SubagentStop, PostCompact, Notification, anything else: noop


__all__ = [
    "ManifestDiff",
    "ReplayConfig",
    "ReplaySummary",
    "diff_manifests",
    "render_diff",
    "replay",
    "replay_to_manifests",
]
