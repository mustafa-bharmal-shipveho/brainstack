"""Rank-list fusion for multi-query retrieval.

When query expansion runs the retriever N times (one per query variant),
each call returns its own ranked list. Fusion merges them into a single
ranking that reflects "which doc shows up consistently AND highly across
variants."

This module implements Reciprocal Rank Fusion (RRF), the standard
algorithm (Cormack, Clarke, Buettcher 2009). Empirical comparison on a
38-query hard set against a real brain showed RRF (k=60) is robust at
preserving Recall@10 while letting a post-hoc cross-encoder reorder the
top entries by relevance to the original query — see
`tests/recall/eval_expansion.py`.
"""
from __future__ import annotations

from typing import Iterable

from recall.core import Document, QueryResult


# Standard RRF constant. k=60 is the value used in the original TREC
# experiments and remains the typical default in retrieval libraries.
RRF_K = 60


def rrf_merge(
    per_variant: list[list[QueryResult]],
    *,
    k: int = RRF_K,
) -> list[QueryResult]:
    """Merge multiple ranked lists via Reciprocal Rank Fusion.

    Each ranked list contributes 1/(k + rank) to each document it ranks.
    Docs appearing in multiple variants accumulate score; docs ranked high
    in any one variant get a meaningful contribution.

    Doc identity is `QueryResult.document.path` (string equality). The
    returned list contains one entry per unique doc, sorted by descending
    fused score. The returned `QueryResult.score` is the RRF fused score
    (NOT a similarity score) — downstream rerankers should ignore it and
    rescore from text + query.

    Returns an empty list if all input lists are empty.
    """
    if not per_variant:
        return []

    path_to_doc: dict[str, Document] = {}
    path_to_score: dict[str, float] = {}

    for ranked in per_variant:
        for rank, qr in enumerate(ranked, start=1):
            p = qr.document.path
            path_to_doc.setdefault(p, qr.document)
            path_to_score[p] = path_to_score.get(p, 0.0) + 1.0 / (k + rank)

    fused = [
        QueryResult(document=path_to_doc[p], score=score)
        for p, score in sorted(path_to_score.items(), key=lambda kv: kv[1], reverse=True)
    ]
    return fused
