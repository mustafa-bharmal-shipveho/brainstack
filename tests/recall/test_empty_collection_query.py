"""Regression: a hybrid query must not crash when a configured source has an
empty (0-point) collection.

Found by the clean-room end-to-end install test: a fresh install always creates
the `imports` source, which is empty until you add a source. The very first
`recall query` then issued a hybrid query against the empty `imports`
collection and crashed inside Qdrant's sparse IDF rescoring (BM25 IDF over an
empty corpus). The real-brain QA missed it because that brain's `imports` tier
had content. This is a first-run crash for new adopters, so it is guarded here.

Marked `embeddings` because reproducing the crash needs the real embedded
Qdrant + sparse model (the same tier as the other index/query tests).
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.embeddings


def test_query_over_empty_and_nonempty_collections_does_not_crash(isolated_xdg):
    from recall.core import Document, HybridRetriever

    docs = [
        Document(
            path="lessons/healthchecks.md",
            source="brain",
            title="Docker healthchecks",
            frontmatter={},
            body="Add a HEALTHCHECK to every compose service and wait on service_healthy.",
            text="Docker healthchecks. Add a HEALTHCHECK to every compose service.",
        ),
    ]
    # Two collections: "brain" gets the doc, "imports" is created empty (the
    # fresh-install shape). Construct with documents so "brain" is populated,
    # then add an empty "imports" collection to the search set.
    retriever = HybridRetriever(documents=docs, collections=["brain"])

    from recall import qdrant_backend as qb
    qb.ensure_collection(retriever._client, "imports")  # exists, 0 points
    retriever._collections = ["brain", "imports"]

    # Before the fix this raised inside _rescore_idf; it must now return the
    # brain hit and simply skip the empty collection.
    results = retriever.query("docker healthcheck compose", k=3)
    assert results, "expected the brain doc, got nothing"
    assert any("healthcheck" in r.document.path.lower()
               or "healthcheck" in r.document.body.lower() for r in results)


def test_query_only_empty_collection_returns_empty_not_crash(isolated_xdg):
    from recall import qdrant_backend as qb
    from recall.core import HybridRetriever

    retriever = HybridRetriever(documents=[], collections=["brain"])
    qb.ensure_collection(retriever._client, "imports")
    retriever._collections = ["imports"]

    # An all-empty search set must return [] cleanly, never raise.
    assert retriever.query("anything", k=5) == []
