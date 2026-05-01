"""Stress-test policy determinism: 100 random events, run each policy 5x,
assert byte-identical output.

Uses a seeded RNG so the test itself is deterministic too. Catches:
  - hidden non-deterministic ordering (e.g., dict iteration leaking through)
  - hidden floating-point drift (Python version specific)
  - any time-based state in the policies
"""
from __future__ import annotations

import random

import pytest

from runtime.core.policy import EvictionRequest, InjectionItem
from runtime.core.policy.defaults.lru import LRUPolicy
from runtime.core.policy.defaults.pinned_first import PinnedFirstPolicy
from runtime.core.policy.defaults.recency_weighted import RecencyWeightedPolicy


def _generate_items(seed: int, n: int) -> list[InjectionItem]:
    rng = random.Random(seed)
    out: list[InjectionItem] = []
    for i in range(n):
        out.append(InjectionItem(
            id=f"c-{i:04d}",
            bucket=rng.choice(["hot", "retrieved", "scratchpad"]),
            token_count=rng.randint(20, 800),
            last_touched_turn=rng.randint(0, 100),
            pinned=rng.random() < 0.1,
            score=rng.random(),
        ))
    return out


@pytest.mark.parametrize("policy_cls", [LRUPolicy, RecencyWeightedPolicy, PinnedFirstPolicy])
@pytest.mark.parametrize("seed", [1, 7, 42, 99])
def test_policy_byte_identical_across_runs(policy_cls, seed: int) -> None:
    items = _generate_items(seed, 100)
    req = EvictionRequest(
        items=items,
        current_turn=200,
        evict_tokens=2000,
        bucket="retrieved",
    )
    runs = [policy_cls().choose_evictions(req) for _ in range(5)]
    assert all(r == runs[0] for r in runs[1:]), (
        f"{policy_cls.__name__} produced non-identical output across 5 runs"
    )


@pytest.mark.parametrize("policy_cls", [LRUPolicy, RecencyWeightedPolicy, PinnedFirstPolicy])
def test_policy_deterministic_across_input_shuffles(policy_cls) -> None:
    """Same set of items in different list orders must produce the same eviction
    decision. This catches accidental dependence on iteration order."""
    rng = random.Random(123)
    items = _generate_items(seed=5, n=50)
    req_a = EvictionRequest(
        items=list(items),
        current_turn=200,
        evict_tokens=1000,
        bucket="retrieved",
    )
    shuffled = list(items)
    rng.shuffle(shuffled)
    req_b = EvictionRequest(
        items=shuffled,
        current_turn=200,
        evict_tokens=1000,
        bucket="retrieved",
    )
    a = policy_cls().choose_evictions(req_a)
    b = policy_cls().choose_evictions(req_b)
    assert sorted(a) == sorted(b), (
        f"{policy_cls.__name__} depends on input order; "
        f"unsorted={a}, shuffled={b}"
    )
