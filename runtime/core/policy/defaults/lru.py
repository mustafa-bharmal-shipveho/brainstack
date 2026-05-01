"""Least-Recently-Used eviction.

Evicts items with the lowest `last_touched_turn` first. Pinned items are
skipped. Stops as soon as the requested token budget is freed.

This is the safest default: predictable, well-understood, easy to reason
about in audit logs.
"""
from __future__ import annotations

from runtime.core.policy import EvictionRequest, filter_to_bucket


class LRUPolicy:
    """Pure LRU. No tie-breaking other than (last_touched_turn, id) ascending."""

    def choose_evictions(self, request: EvictionRequest) -> list[str]:
        if request.evict_tokens <= 0:
            return []

        candidates = [it for it in filter_to_bucket(request.items, request.bucket) if not it.pinned]
        # Oldest first, then by id for stable ordering across runs/locales.
        candidates.sort(key=lambda it: (it.last_touched_turn, it.id))

        chosen: list[str] = []
        freed = 0
        for it in candidates:
            chosen.append(it.id)
            freed += it.token_count
            if freed >= request.evict_tokens:
                break
        return chosen
