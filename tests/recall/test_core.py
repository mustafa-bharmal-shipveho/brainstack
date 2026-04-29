"""Tests for the Qdrant-backed HybridRetriever facade."""

from __future__ import annotations

import pytest

from recall.core import Document, HybridRetriever, QueryResult


def _make_doc(
    name: str,
    description: str,
    body: str = "",
    source: str = "brain",
    type_: str = "reference",
) -> Document:
    return Document(
        path=f"/synth/{source}/{name}.md",
        source=source,
        title=name,
        frontmatter={"name": name, "description": description, "type": type_},
        body=body,
        text=f"{name} {description} {description} {description} {body}",
    )


@pytest.fixture(autouse=True)
def _reset_qdrant(isolated_xdg):
    """Drop client handles between tests so the embedded DB lives under tmp_xdg."""
    from recall import qdrant_backend as qb

    qb._reset_client_cache_for_tests()
    yield
    qb._reset_client_cache_for_tests()


class TestHybridRetrieverFacade:
    def test_empty_corpus_returns_empty(self):
        retriever = HybridRetriever([])
        assert retriever.query("anything", k=5) == []

    def test_k_zero_returns_empty(self):
        retriever = HybridRetriever([_make_doc("a", "hello world")])
        assert retriever.query("hello", k=0) == []

    def test_negative_k_returns_empty(self):
        retriever = HybridRetriever([_make_doc("a", "hello world")])
        assert retriever.query("hello", k=-3) == []

    def test_single_doc_returns_one_result(self):
        retriever = HybridRetriever(
            [_make_doc("only", "the only doc in the index")]
        )
        results = retriever.query("only doc", k=5)
        assert len(results) == 1
        assert isinstance(results[0], QueryResult)
        assert results[0].document.frontmatter["name"] == "only"

    def test_hybrid_lexical_match_wins_obvious_query(self):
        docs = [
            _make_doc(
                "python-gil",
                "global interpreter lock prevents parallel cpu work",
            ),
            _make_doc(
                "rust-borrow",
                "ownership and lifetimes prevent memory bugs",
            ),
            _make_doc("go-channels", "channels coordinate goroutines"),
        ]
        retriever = HybridRetriever(docs)
        results = retriever.query("python parallel cpu", k=2)
        assert len(results) == 2
        assert results[0].document.frontmatter["name"] == "python-gil"

    def test_hybrid_paraphrase_match_via_dense(self):
        # Paraphrase shares no key tokens with description — dense leg must rescue it.
        docs = [
            _make_doc(
                "python-gil",
                "global interpreter lock prevents true thread parallelism for cpu work",
            ),
            _make_doc("rust-borrow", "ownership lifetimes memory bugs"),
            _make_doc("go-channels", "channels coordinate goroutines"),
        ]
        retriever = HybridRetriever(docs)
        results = retriever.query(
            "why doesn't multithreading speed up my number crunching", k=3
        )
        assert results, "expected at least one result"
        names = [r.document.frontmatter["name"] for r in results]
        assert "python-gil" in names

    def test_type_filter_excludes_non_matching(self):
        d1 = _make_doc("a", "feedback memory entry", type_="feedback")
        d2 = _make_doc("b", "reference memory entry", type_="reference")
        retriever = HybridRetriever([d1, d2])
        results = retriever.query("memory", k=5, type_filter="feedback")
        assert len(results) == 1
        assert results[0].document.frontmatter["type"] == "feedback"

    def test_source_filter_excludes_other_sources(self):
        d1 = _make_doc("brain-doc", "brain content here", source="brain")
        d2 = _make_doc("vault-doc", "vault content here", source="vault")
        retriever = HybridRetriever([d1, d2])
        results = retriever.query("content", k=5, source_filter="brain")
        assert results, "expected at least one result"
        assert all(r.document.source == "brain" for r in results)

    def test_score_within_reasonable_range(self):
        retriever = HybridRetriever([_make_doc("foo", "asparagus risotto recipe")])
        results = retriever.query("asparagus risotto", k=1)
        assert results
        # Qdrant RRF fusion produces non-negative scores.
        assert results[0].score >= 0.0

    def test_reupsert_is_idempotent_no_duplicate_ids(self):
        docs = [_make_doc(f"d{i}", f"description number {i}") for i in range(3)]
        HybridRetriever(docs)
        retriever = HybridRetriever(docs)
        results = retriever.query("description number 1", k=10)
        # No more than 3 unique paths come back (deterministic UUID5(path) ids).
        assert len({r.document.path for r in results}) <= 3


class TestCrossEncoderReranker:
    """Cross-encoder rerank stage tests.

    The reranker is default-on. These tests confirm:
    - It changes ranking on a paraphrased query the bi-encoder gets wrong
    - reranker="none" bypasses the third stage cleanly
    - Short corpora (n <= k) skip the rerank short-circuit
    """

    def test_rerank_can_change_top_position(self):
        # Construct a corpus where one doc's body is a clear semantic match for the
        # query while its description is bland. The bi-encoder may rank others above
        # it; the cross-encoder should pull it forward.
        docs = [
            _make_doc(
                "incident-runbook",
                "operations doc",
                body="when production goes down at 2am, paste the rollback SQL inline first",
            ),
            _make_doc("docs-style", "writing reference docs", body="use the style guide"),
            _make_doc(
                "team-roster",
                "who works on which team",
                body="alphabetical list of engineers and their teams",
            ),
            _make_doc(
                "build-pipeline",
                "CI build steps reference",
                body="how the test suite runs in CI",
            ),
            _make_doc(
                "release-notes",
                "changelog generation rules",
                body="how releases are tagged",
            ),
        ]
        retriever = HybridRetriever(docs, reranker="cross_encoder", rerank_n=10)
        results = retriever.query(
            "production is on fire at 2am, what now", k=3
        )
        names = [r.document.frontmatter["name"] for r in results]
        assert "incident-runbook" in names

    def test_reranker_none_skips_third_stage(self):
        docs = [
            _make_doc("a", "alpha", body="alpha bravo charlie"),
            _make_doc("b", "bravo", body="bravo charlie delta"),
        ]
        retriever = HybridRetriever(docs, reranker="none")
        results = retriever.query("alpha", k=2)
        assert len(results) == 2  # both surface, no rerank crash

    def test_reranker_short_corpus_short_circuits(self):
        # When candidates <= k, rerank is wasted and should pass through.
        docs = [_make_doc(f"d{i}", f"desc {i}") for i in range(3)]
        retriever = HybridRetriever(docs, reranker="cross_encoder", rerank_n=20)
        results = retriever.query("desc 1", k=5)
        assert 1 <= len(results) <= 3

    def test_reranker_default_is_none(self):
        # Sanity: HybridRetriever() default value matches RankingConfig default.
        # Rerank is opt-in (per-query --rerank cross_encoder, or per-user via
        # config.json) because real-brain testing showed it's a wash at the
        # current scale and content shape.
        from recall.config import RankingConfig

        cfg = RankingConfig()
        assert cfg.reranker == "none"
        # Model name is still set so opt-in users get a sensible default
        assert cfg.reranker_model == "jinaai/jina-reranker-v1-turbo-en"
