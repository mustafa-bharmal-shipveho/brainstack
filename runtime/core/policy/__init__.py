"""Eviction policies for the runtime.

A policy is a pure function: given the current set of injected items and a
request to free up N tokens from a target bucket, it returns an ordered list
of item IDs to demote-from-injection on the next turn.

Policies must be:
  - **Pure.** No I/O. No clock reads. No randomness. No mutation of inputs.
  - **Deterministic.** Same input -> same output, every time, every machine.
  - **Bucket-scoped.** Only consider items whose `bucket` matches the request.

These constraints exist so replay/audit is meaningful: if turn 38's manifest
says LRU evicted item X, replaying turn 38's events through LRU MUST again
choose item X. Anything fuzzy breaks the audit guarantee.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence, runtime_checkable


@dataclass(frozen=True)
class InjectionItem:
    """A single thing currently in the injected context.

    Frozen so policies cannot accidentally mutate it. Add fields here only
    when the runtime needs them across all policies; per-policy state lives
    in `score` (a free float each policy interprets as it likes) or in the
    item id (referencing a shared store).
    """

    id: str
    bucket: str
    token_count: int
    last_touched_turn: int
    pinned: bool = False
    score: float = 0.0


@dataclass(frozen=True)
class EvictionRequest:
    """One eviction question the runtime asks the policy.

    `items` is the full snapshot (including items in other buckets); the
    policy is responsible for filtering to `bucket`. This shape lets one
    policy implementation see the whole picture if it ever needs cross-bucket
    awareness, without forcing every policy to reimplement that.
    """

    items: Sequence[InjectionItem]
    current_turn: int
    evict_tokens: int
    bucket: str


@runtime_checkable
class Policy(Protocol):
    """Anything with a `choose_evictions(request) -> list[str]` method."""

    def choose_evictions(self, request: EvictionRequest) -> list[str]: ...


def filter_to_bucket(items: Sequence[InjectionItem], bucket: str) -> list[InjectionItem]:
    """Helper used by every default policy."""
    return [it for it in items if it.bucket == bucket]


__all__ = [
    "EvictionRequest",
    "InjectionItem",
    "Policy",
    "filter_to_bucket",
]
