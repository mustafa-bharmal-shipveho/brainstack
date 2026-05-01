"""Synthetic budget-overflow test.

Verifies eviction triggers at exactly cap+1 token, and that the chosen
eviction set frees enough tokens to bring usage back below cap.
"""
from __future__ import annotations

import pytest

from runtime.core.policy import EvictionRequest, InjectionItem
from runtime.core.policy.defaults.lru import LRUPolicy


def _items_filling_to(cap: int) -> list[InjectionItem]:
    """Items totaling exactly `cap` tokens, oldest first."""
    out: list[InjectionItem] = []
    remaining = cap
    i = 0
    while remaining > 0:
        size = min(100, remaining)
        out.append(InjectionItem(
            id=f"c-{i:04d}",
            bucket="retrieved",
            token_count=size,
            last_touched_turn=i,
            pinned=False,
        ))
        remaining -= size
        i += 1
    return out


def test_no_eviction_at_exactly_cap() -> None:
    """At cap exactly, evict_tokens=0; policy returns []. The runtime never
    runs eviction unless evict_tokens > 0."""
    items = _items_filling_to(1000)
    req = EvictionRequest(items=items, current_turn=10, evict_tokens=0, bucket="retrieved")
    assert LRUPolicy().choose_evictions(req) == []


def test_eviction_at_cap_plus_one() -> None:
    """At cap+1 over, evict_tokens=1; policy chooses at least 1 token's worth.
    With 100-token items, that's exactly 1 item: the oldest."""
    items = _items_filling_to(1000)
    req = EvictionRequest(items=items, current_turn=10, evict_tokens=1, bucket="retrieved")
    chosen = LRUPolicy().choose_evictions(req)
    assert chosen == ["c-0000"]


def test_eviction_chooses_enough_to_clear_overflow() -> None:
    """If we're over by 250 tokens, the chosen set must free >= 250."""
    items = _items_filling_to(2000)
    req = EvictionRequest(items=items, current_turn=20, evict_tokens=250, bucket="retrieved")
    chosen = LRUPolicy().choose_evictions(req)
    freed = sum(it.token_count for it in items if it.id in chosen)
    assert freed >= 250
    # And ideally not wildly more (LRU stops as soon as budget is met)
    assert freed - 250 < 100  # at most one item's worth of slack


def test_eviction_when_all_remaining_pinned() -> None:
    """If everything not yet evicted is pinned, LRU returns the empty set
    (the runtime then handles the impasse)."""
    items = [
        InjectionItem(id="p-0", bucket="retrieved", token_count=500, last_touched_turn=0, pinned=True),
        InjectionItem(id="p-1", bucket="retrieved", token_count=500, last_touched_turn=1, pinned=True),
    ]
    req = EvictionRequest(items=items, current_turn=10, evict_tokens=200, bucket="retrieved")
    assert LRUPolicy().choose_evictions(req) == []


@pytest.mark.parametrize("overflow", [1, 50, 99, 100, 101, 250, 1000])
def test_eviction_freed_tokens_meets_or_exceeds_overflow(overflow: int) -> None:
    items = _items_filling_to(2000)
    req = EvictionRequest(items=items, current_turn=20, evict_tokens=overflow, bucket="retrieved")
    chosen = LRUPolicy().choose_evictions(req)
    freed = sum(it.token_count for it in items if it.id in chosen)
    assert freed >= overflow
