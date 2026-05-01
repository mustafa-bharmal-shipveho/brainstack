"""Recency-weighted eviction.

Evicts items with the lowest `score` first. The runtime is expected to
populate `score` upstream (e.g., retrieval rank * recency decay). Ties are
broken by older `last_touched_turn`, then by id.

Use when you want eviction to honor a relevance signal richer than just
"how long since I touched it." Example: a frequently-cited lesson with a
high score can survive even if its last_touched_turn is old.
"""
from __future__ import annotations

from runtime.core.policy import EvictionRequest, filter_to_bucket


class RecencyWeightedPolicy:
    """Sort ascending by (score, last_touched_turn, id). Pinned skipped."""

    def choose_evictions(self, request: EvictionRequest) -> list[str]:
        if request.evict_tokens <= 0:
            return []

        candidates = [it for it in filter_to_bucket(request.items, request.bucket) if not it.pinned]
        candidates.sort(key=lambda it: (it.score, it.last_touched_turn, it.id))

        chosen: list[str] = []
        freed = 0
        for it in candidates:
            chosen.append(it.id)
            freed += it.token_count
            if freed >= request.evict_tokens:
                break
        return chosen
