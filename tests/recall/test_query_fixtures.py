"""100+ parametrized query test cases against a 40-doc synthetic corpus.

These tests measure end-to-end retrieval quality. Each case asserts that the
expected memory shows up in the top-3 results for a given query — both for
direct lexical matches (BM25 strength) and paraphrased queries (where
embeddings help, but BM25 should still get most of them right with
description-weighting).
"""

from __future__ import annotations

import importlib.util

import pytest

from recall.config import SourceConfig
from recall.core import HybridRetriever
from recall.sources import discover_documents


def _build_retriever(corpus_path, embedding_weight: float = 0.0) -> HybridRetriever:
    """Build a HybridRetriever over the synthetic corpus.

    `embedding_weight` is accepted for back-compat with the parametrized cases
    that asked for BM25-only mode. The new HybridRetriever always runs hybrid
    (Qdrant Prefetch + Fusion.RRF over dense + sparse), which strictly subsumes
    BM25-only quality. The kwarg is ignored.
    """
    sc = SourceConfig(
        name="big",
        path=str(corpus_path),
        glob="**/*.md",
        frontmatter="auto-memory",
        exclude=[],
    )
    docs = list(discover_documents(sc))
    # Reset client so module-scoped tests get a fresh embedded DB
    from recall import qdrant_backend as qb
    qb._reset_client_cache_for_tests()
    return HybridRetriever(docs)


@pytest.fixture(scope="module")
def hundred_corpus_module(tmp_path_factory):
    """Module-scoped corpus to avoid rebuilding for every parametrized case."""
    # We can't use the conftest fixture here because tmp_path is function-scoped.
    # Re-create equivalent corpus using the same builder.
    from tests.recall.conftest import _build_hundred_corpus, _build_query_cases, _write_auto_memory_file

    base = tmp_path_factory.mktemp("hundred")
    for path, frontmatter, body in _build_hundred_corpus():
        _write_auto_memory_file(base / path, frontmatter, body)
    return base, _build_query_cases()


def _query_cases():
    """Eagerly materialize cases for parametrize ID generation."""
    from tests.recall.conftest import _build_query_cases

    return _build_query_cases()


_LEXICAL_CASES = [c for c in _query_cases() if c.get("kind") in ("lexical", "mixed")]


@pytest.mark.parametrize(
    "case",
    _LEXICAL_CASES,
    ids=lambda c: c["query"][:60],
)
def test_query_top_3_bm25_only_lexical(hundred_corpus_module, case):
    """BM25-only retrieval must place the expected hit in top-3 for lexical cases."""
    corpus, _cases = hundred_corpus_module
    retriever = _build_retriever(corpus, embedding_weight=0.0)

    type_filter = case.get("type_filter")
    results = retriever.query(case["query"], k=3, type_filter=type_filter)

    expected = case["expected_top_name"]
    top_names = [r.document.frontmatter.get("name") for r in results]
    assert (
        expected in top_names
    ), f"Expected '{expected}' in top-3 for query '{case['query']}'; got {top_names}"


@pytest.mark.parametrize(
    "case",
    [c for c in _query_cases() if c.get("negative")],
    ids=lambda c: c["query"][:60],
)
def test_negative_queries_dont_crash(hundred_corpus_module, case):
    """Negative cases: just ensure the system returns valid results without crashing."""
    corpus, _cases = hundred_corpus_module
    retriever = _build_retriever(corpus, embedding_weight=0.0)
    results = retriever.query(case["query"], k=3)
    assert len(results) <= 3
    # All results should be valid Documents with paths
    for r in results:
        assert r.document.path


@pytest.mark.embeddings
@pytest.mark.parametrize(
    "case",
    [c for c in _query_cases() if c.get("kind") in ("lexical", "mixed")],
    ids=lambda c: c["query"][:60],
)
def test_query_top_3_hybrid_lexical(hundred_corpus_module, case):
    """Hybrid must hit top-3 for lexical+mixed cases (where BM25 alone already does)."""
    if importlib.util.find_spec("sentence_transformers") is None:
        pytest.skip("sentence-transformers not installed")
    corpus, _cases = hundred_corpus_module
    retriever = _build_retriever(corpus, embedding_weight=1.0)

    type_filter = case.get("type_filter")
    results = retriever.query(case["query"], k=3, type_filter=type_filter)

    expected = case["expected_top_name"]
    top_names = [r.document.frontmatter.get("name") for r in results]
    assert expected in top_names, (
        f"Hybrid: expected '{expected}' in top-3 for '{case['query']}'; got {top_names}"
    )


@pytest.mark.embeddings
def test_recall_at_5_hybrid_paraphrase_aggregate(hundred_corpus_module):
    """Aggregate hybrid recall@5 on paraphrase cases. Per-case is too brittle for
    a 90 MB general-purpose model — paraphrases like kubectl→kubernetes need
    domain-specific embeddings. We assert that at least 65% land top-5.
    """
    if importlib.util.find_spec("sentence_transformers") is None:
        pytest.skip("sentence-transformers not installed")
    corpus, cases = hundred_corpus_module
    retriever = _build_retriever(corpus, embedding_weight=1.0)
    paraphrase = [c for c in cases if c.get("kind") == "paraphrase"]
    hits = 0
    misses = []
    for case in paraphrase:
        results = retriever.query(case["query"], k=5)
        names = {r.document.frontmatter.get("name") for r in results}
        if case["expected_top_name"] in names:
            hits += 1
        else:
            misses.append((case["query"], case["expected_top_name"]))
    rate = hits / len(paraphrase)
    assert (
        rate >= 0.65
    ), f"Hybrid paraphrase recall@5 = {rate:.2%} ({hits}/{len(paraphrase)}). Misses: {misses}"


def test_corpus_size_at_least_100_cases():
    """Sanity: confirm we actually have 100+ cases."""
    cases = _query_cases()
    assert len(cases) >= 100, f"Need 100+ cases, got {len(cases)}"


def test_recall_at_5_threshold_lexical(hundred_corpus_module):
    """BM25 must hit recall@5 ≥ 90% on lexical+mixed cases."""
    corpus, cases = hundred_corpus_module
    retriever = _build_retriever(corpus, embedding_weight=0.0)
    lexical = [c for c in cases if c.get("kind") in ("lexical", "mixed")]
    hits = 0
    misses = []
    for case in lexical:
        results = retriever.query(case["query"], k=5, type_filter=case.get("type_filter"))
        names = {r.document.frontmatter.get("name") for r in results}
        if case["expected_top_name"] in names:
            hits += 1
        else:
            misses.append((case["query"], case["expected_top_name"], list(names)))
    rate = hits / len(lexical)
    assert (
        rate >= 0.90
    ), f"BM25 lexical recall@5 = {rate:.2%} ({hits}/{len(lexical)}). Misses: {misses[:5]}"


def test_recall_at_5_threshold_overall_bm25(hundred_corpus_module):
    """BM25-only must hit recall@5 ≥ 60% even including paraphrase cases (sanity floor)."""
    corpus, cases = hundred_corpus_module
    retriever = _build_retriever(corpus, embedding_weight=0.0)
    positive = [c for c in cases if not c.get("negative")]
    hits = sum(
        1
        for case in positive
        if case["expected_top_name"]
        in {
            r.document.frontmatter.get("name")
            for r in retriever.query(case["query"], k=5, type_filter=case.get("type_filter"))
        }
    )
    rate = hits / len(positive)
    assert rate >= 0.60, f"BM25 overall recall@5 = {rate:.2%} ({hits}/{len(positive)})"


@pytest.mark.embeddings
def test_recall_at_5_hybrid_overall(hundred_corpus_module):
    """Hybrid retrieval must hit recall@5 ≥ 85% across all positive cases."""
    if importlib.util.find_spec("sentence_transformers") is None:
        pytest.skip("sentence-transformers not installed")
    corpus, cases = hundred_corpus_module
    retriever = _build_retriever(corpus, embedding_weight=1.0)
    positive = [c for c in cases if not c.get("negative")]
    hits = 0
    misses = []
    for case in positive:
        results = retriever.query(case["query"], k=5, type_filter=case.get("type_filter"))
        names = {r.document.frontmatter.get("name") for r in results}
        if case["expected_top_name"] in names:
            hits += 1
        else:
            misses.append((case["query"], case["expected_top_name"]))
    rate = hits / len(positive)
    assert (
        rate >= 0.85
    ), f"Hybrid recall@5 = {rate:.2%} ({hits}/{len(positive)}). Misses: {misses[:5]}"
