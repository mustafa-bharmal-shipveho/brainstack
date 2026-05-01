"""Sub-phase 1c: Policy contract + LRU/recency/pinned-first default tests.

Policies are pure functions: events in, eviction decisions out. No I/O, no
clock reads, no global state. Same input MUST always produce the same output;
otherwise replay/audit (sub-phase 3f) breaks.
"""
from __future__ import annotations

import pytest

from runtime.core.policy import EvictionRequest, InjectionItem, Policy
from runtime.core.policy.defaults.lru import LRUPolicy
from runtime.core.policy.defaults.pinned_first import PinnedFirstPolicy
from runtime.core.policy.defaults.recency_weighted import RecencyWeightedPolicy


def _item(
    id: str,
    *,
    bucket: str = "retrieved",
    tokens: int = 100,
    last: int = 0,
    pinned: bool = False,
    score: float = 1.0,
) -> InjectionItem:
    return InjectionItem(
        id=id,
        bucket=bucket,
        token_count=tokens,
        last_touched_turn=last,
        pinned=pinned,
        score=score,
    )


def _req(items: list[InjectionItem], current_turn: int, evict_tokens: int, bucket: str = "retrieved") -> EvictionRequest:
    return EvictionRequest(
        items=items,
        current_turn=current_turn,
        evict_tokens=evict_tokens,
        bucket=bucket,
    )


# ---------- protocol conformance ----------

def test_lru_satisfies_policy_protocol() -> None:
    p: Policy = LRUPolicy()
    assert callable(p.choose_evictions)


def test_recency_satisfies_policy_protocol() -> None:
    p: Policy = RecencyWeightedPolicy()
    assert callable(p.choose_evictions)


def test_pinned_satisfies_policy_protocol() -> None:
    p: Policy = PinnedFirstPolicy()
    assert callable(p.choose_evictions)


# ---------- determinism ----------

@pytest.mark.parametrize("policy_cls", [LRUPolicy, RecencyWeightedPolicy, PinnedFirstPolicy])
def test_policy_is_deterministic(policy_cls) -> None:
    items = [_item(f"id-{i}", last=i % 5, tokens=50) for i in range(20)]
    req = _req(items, current_turn=10, evict_tokens=300)
    a = policy_cls().choose_evictions(req)
    b = policy_cls().choose_evictions(req)
    c = policy_cls().choose_evictions(req)
    assert a == b == c, f"{policy_cls.__name__} is not deterministic"


@pytest.mark.parametrize("policy_cls", [LRUPolicy, RecencyWeightedPolicy, PinnedFirstPolicy])
def test_policy_evicts_at_least_requested_tokens(policy_cls) -> None:
    """The chosen eviction set's total tokens must be >= the requested cut.

    Otherwise the budget is not actually enforced after the policy runs."""
    items = [_item(f"id-{i}", tokens=100, last=i) for i in range(10)]
    req = _req(items, current_turn=10, evict_tokens=350)
    chosen_ids = policy_cls().choose_evictions(req)
    chosen_tokens = sum(it.token_count for it in items if it.id in chosen_ids)
    assert chosen_tokens >= 350


@pytest.mark.parametrize("policy_cls", [LRUPolicy, RecencyWeightedPolicy, PinnedFirstPolicy])
def test_policy_returns_empty_when_no_eviction_needed(policy_cls) -> None:
    items = [_item("a"), _item("b")]
    req = _req(items, current_turn=5, evict_tokens=0)
    assert policy_cls().choose_evictions(req) == []


@pytest.mark.parametrize("policy_cls", [LRUPolicy, RecencyWeightedPolicy, PinnedFirstPolicy])
def test_policy_does_not_mutate_input(policy_cls) -> None:
    items = [_item(f"id-{i}", last=i, tokens=50) for i in range(5)]
    snapshot = [
        (it.id, it.bucket, it.token_count, it.last_touched_turn, it.pinned, it.score)
        for it in items
    ]
    policy_cls().choose_evictions(_req(items, current_turn=10, evict_tokens=100))
    after = [
        (it.id, it.bucket, it.token_count, it.last_touched_turn, it.pinned, it.score)
        for it in items
    ]
    assert snapshot == after


@pytest.mark.parametrize("policy_cls", [LRUPolicy, RecencyWeightedPolicy, PinnedFirstPolicy])
def test_policy_filters_to_target_bucket(policy_cls) -> None:
    """Evictions only come from the bucket the request targets, not from
    other buckets that happen to be in the items list."""
    items = [
        _item("hot-1", bucket="hot", tokens=100, last=0),
        _item("ret-1", bucket="retrieved", tokens=100, last=0),
        _item("ret-2", bucket="retrieved", tokens=100, last=1),
    ]
    req = _req(items, current_turn=5, evict_tokens=100, bucket="retrieved")
    chosen = policy_cls().choose_evictions(req)
    assert "hot-1" not in chosen
    assert all(cid.startswith("ret-") for cid in chosen)


# ---------- LRU specifics ----------

def test_lru_evicts_oldest_first() -> None:
    items = [
        _item("oldest", last=0, tokens=100),
        _item("middle", last=5, tokens=100),
        _item("newest", last=10, tokens=100),
    ]
    chosen = LRUPolicy().choose_evictions(_req(items, current_turn=11, evict_tokens=100))
    assert chosen == ["oldest"]


def test_lru_skips_pinned_items() -> None:
    items = [
        _item("pinned-old", last=0, tokens=100, pinned=True),
        _item("middle", last=5, tokens=100),
        _item("newest", last=10, tokens=100),
    ]
    chosen = LRUPolicy().choose_evictions(_req(items, current_turn=11, evict_tokens=100))
    assert "pinned-old" not in chosen
    assert chosen == ["middle"]


def test_lru_evicts_multiple_until_budget_met() -> None:
    items = [_item(f"item-{i}", last=i, tokens=50) for i in range(10)]
    chosen = LRUPolicy().choose_evictions(_req(items, current_turn=11, evict_tokens=120))
    # Must cut at least 120 tokens; 50-token items, so >=3 items
    assert len(chosen) >= 3
    # Should evict the lowest last_touched_turn first
    assert chosen[:3] == ["item-0", "item-1", "item-2"]


# ---------- recency-weighted specifics ----------

def test_recency_evicts_lowest_score_first() -> None:
    """Score is the policy's combined value; lower = more evictable."""
    items = [
        _item("low",  score=0.1, tokens=100, last=5),
        _item("mid",  score=0.5, tokens=100, last=5),
        _item("high", score=0.9, tokens=100, last=5),
    ]
    chosen = RecencyWeightedPolicy().choose_evictions(_req(items, current_turn=10, evict_tokens=100))
    assert chosen == ["low"]


def test_recency_breaks_ties_with_age() -> None:
    """Equal scores: older (smaller last_touched_turn) goes first."""
    items = [
        _item("equal-old",   score=0.5, tokens=100, last=1),
        _item("equal-young", score=0.5, tokens=100, last=8),
    ]
    chosen = RecencyWeightedPolicy().choose_evictions(_req(items, current_turn=10, evict_tokens=100))
    assert chosen == ["equal-old"]


# ---------- pinned-first specifics ----------

def test_pinned_first_never_evicts_pinned() -> None:
    """Pinned items must NEVER be in the eviction set, even if budget can't
    be met without them. The runtime then has to find another bucket to cut."""
    items = [
        _item("p1", pinned=True, tokens=100, last=0),
        _item("p2", pinned=True, tokens=100, last=1),
        _item("p3", pinned=True, tokens=100, last=2),
    ]
    chosen = PinnedFirstPolicy().choose_evictions(_req(items, current_turn=10, evict_tokens=200))
    assert chosen == []  # all pinned -> no evictions, even if budget is breached


def test_pinned_first_evicts_unpinned_lru() -> None:
    items = [
        _item("p1",      pinned=True, tokens=100, last=0),
        _item("free-old",            tokens=100, last=2),
        _item("free-mid",            tokens=100, last=5),
        _item("free-new",            tokens=100, last=9),
    ]
    chosen = PinnedFirstPolicy().choose_evictions(_req(items, current_turn=10, evict_tokens=100))
    assert chosen == ["free-old"]
