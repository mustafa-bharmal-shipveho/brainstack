"""Pinned-first eviction.

Pinned items are sacred: this policy NEVER evicts them, even if the budget
cannot be met without them. Among unpinned items, evicts LRU.

If the policy can't free enough tokens (because everything in the target
bucket is pinned), it returns whatever it could, including possibly the
empty list. The runtime treats that as a signal to either accept the
overflow, evict from a different bucket, or escalate.

Use this when the user explicitly opts in to "these items must always be
in the model's context." Common case: a CLAUDE.md section that codifies
the team's coding conventions.
"""
from __future__ import annotations

from runtime.core.policy import EvictionRequest, filter_to_bucket


class PinnedFirstPolicy:
    """LRU over the unpinned subset. Pinned items are untouchable."""

    def choose_evictions(self, request: EvictionRequest) -> list[str]:
        if request.evict_tokens <= 0:
            return []

        unpinned = [it for it in filter_to_bucket(request.items, request.bucket) if not it.pinned]
        unpinned.sort(key=lambda it: (it.last_touched_turn, it.id))

        chosen: list[str] = []
        freed = 0
        for it in unpinned:
            chosen.append(it.id)
            freed += it.token_count
            if freed >= request.evict_tokens:
                break
        return chosen
