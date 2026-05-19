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
    pin_first_variant_top: bool = False,
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

    Pure RRF can hurt Recall@1 in query-expansion settings: a doc that
    appears at rank 3 across all variants beats a doc that's rank 1 for
    the *original* query alone, because the cross-variant boost compounds.
    On the maintainer's real-brain hard set, pure RRF dropped Recall@1
    from 13.2% (baseline) to 5.3%. The optional `pin_first_variant_top`
    flag fixes that by forcing the first variant's top-1 result to
    position 0 in the output (treating it as the "authoritative" top-1)
    while letting RRF score the rest. Result: Recall@1 floors at the
    baseline number, Recall@5/@10 still get the expansion lift.

    Callers using query expansion should pass per_variant in the order
    [original_query_results, paraphrase_1_results, ...] so the first list
    *is* the original query when this flag is on.
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

    if pin_first_variant_top and per_variant[0]:
        anchor_path = per_variant[0][0].document.path
        if fused and fused[0].document.path != anchor_path:
            # Find the anchor in the fused list and move it to position 0.
            for i, qr in enumerate(fused):
                if qr.document.path == anchor_path:
                    fused.insert(0, fused.pop(i))
                    break

    return fused
