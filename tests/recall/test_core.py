"""Tests for the retriever core: BM25, embeddings (optional), RRF fusion."""

from __future__ import annotations

import importlib.util

import pytest

from recall.core import (
    Bm25Retriever,
    Document,
    HybridRetriever,
    QueryResult,
    reciprocal_rank_fusion,
)


def _make_doc(name: str, description: str, body: str = "", source: str = "brain") -> Document:
    return Document(
        path=f"/synth/{name}.md",
        source=source,
        title=name,
        frontmatter={"name": name, "description": description, "type": "reference"},
        body=body,
        text=f"{name} {description} {description} {description} {body}",
    )


class TestRRF:
    def test_basic_fusion(self):
        rank_a = ["a", "b", "c"]
        rank_b = ["b", "a", "c"]
        fused = reciprocal_rank_fusion([rank_a, rank_b])
        # 'a' and 'b' should be at top; 'c' last
        assert fused[0] in ("a", "b")
        assert fused[-1] == "c"

    def test_only_in_one_ranking(self):
        rank_a = ["a", "b"]
        rank_b = ["c"]
        fused = reciprocal_rank_fusion([rank_a, rank_b])
        assert set(fused) == {"a", "b", "c"}

    def test_empty_rankings(self):
        assert reciprocal_rank_fusion([]) == []
        assert reciprocal_rank_fusion([[]]) == []
        assert reciprocal_rank_fusion([[], []]) == []

    def test_single_ranking_preserved(self):
        rank = ["a", "b", "c"]
        fused = reciprocal_rank_fusion([rank])
        assert fused == ["a", "b", "c"]

    def test_k_parameter(self):
        # Larger k should de-emphasize rank differences
        rank_a = ["x", "y", "z"]
        rank_b = ["z", "y", "x"]
        fused_default = reciprocal_rank_fusion([rank_a, rank_b])
        fused_large_k = reciprocal_rank_fusion([rank_a, rank_b], k=10000)
        # Both produce valid orderings of all three
        assert set(fused_default) == set(fused_large_k) == {"x", "y", "z"}


class TestBm25Retriever:
    def test_top_k_returns_correct_count(self):
        docs = [
            _make_doc("python-gil", "global interpreter lock prevents parallel cpu work"),
            _make_doc("rust-borrow", "ownership and lifetimes prevent memory bugs"),
            _make_doc("go-channels", "channels coordinate goroutines"),
        ]
        retriever = Bm25Retriever(docs)
        results = retriever.query("python parallel cpu", k=2)
        assert len(results) == 2

    def test_top_match_has_strongest_lexical_overlap(self):
        docs = [
            _make_doc("python-gil", "global interpreter lock prevents parallel cpu work"),
            _make_doc("rust-borrow", "ownership and lifetimes prevent memory bugs"),
            _make_doc("go-channels", "channels coordinate goroutines"),
        ]
        retriever = Bm25Retriever(docs)
        results = retriever.query("python parallel cpu", k=3)
        assert results[0].document.frontmatter["name"] == "python-gil"

    def test_description_weighted_higher(self):
        # Same term in description vs body — description should rank higher
        d1 = _make_doc(
            "in-description",
            "asparagus risotto recipe",  # term in description
            body="generic body without the term",
        )
        d2 = _make_doc(
            "in-body-only",
            "irrelevant header",
            body="asparagus risotto recipe is somewhere in the body but not in description",
        )
        retriever = Bm25Retriever([d1, d2])
        results = retriever.query("asparagus risotto", k=2)
        assert results[0].document.frontmatter["name"] == "in-description"

    def test_empty_corpus_returns_empty(self):
        retriever = Bm25Retriever([])
        assert retriever.query("anything", k=5) == []

    def test_query_no_matches_returns_results_anyway(self):
        # BM25 returns *some* score even for poor matches; should yield up to k
        docs = [_make_doc("foo", "bar baz quux")]
        retriever = Bm25Retriever(docs)
        results = retriever.query("totally unrelated terms", k=5)
        # At most k=1 (only 1 doc) and result may have low score, but call must not crash
        assert len(results) <= 1

    def test_unicode_query_does_not_crash(self):
        docs = [_make_doc("café", "espresso doppio")]
        retriever = Bm25Retriever(docs)
        results = retriever.query("café", k=1)
        assert len(results) == 1

    def test_result_includes_score(self):
        docs = [_make_doc("foo", "bar baz")]
        retriever = Bm25Retriever(docs)
        results = retriever.query("bar", k=1)
        assert hasattr(results[0], "score")
        assert isinstance(results[0].score, float)


@pytest.mark.embeddings
class TestEmbeddings:
    @pytest.fixture(autouse=True)
    def _skip_if_unavailable(self):
        if importlib.util.find_spec("sentence_transformers") is None:
            pytest.skip("sentence-transformers not installed")

    def test_embedding_retriever_paraphrase_match(self):
        from recall.core import EmbeddingRetriever

        docs = [
            _make_doc(
                "python-gil",
                "global interpreter lock prevents true thread parallelism for cpu work",
            ),
            _make_doc("rust-borrow", "ownership and lifetimes"),
        ]
        retriever = EmbeddingRetriever(docs)
        # Paraphrase: no shared terms with description
        results = retriever.query("why doesn't multithreading speed up my number crunching", k=2)
        assert results[0].document.frontmatter["name"] == "python-gil"


class TestHybridRetriever:
    def test_falls_back_to_bm25_when_no_embeddings(self, monkeypatch):
        # Force the no-embeddings code path by configuring weight=0
        docs = [
            _make_doc("python-gil", "global interpreter lock parallel cpu"),
            _make_doc("rust-borrow", "ownership lifetimes memory bugs"),
        ]
        retriever = HybridRetriever(docs, embedding_weight=0.0)
        results = retriever.query("python cpu", k=2)
        assert results[0].document.frontmatter["name"] == "python-gil"

    def test_returns_query_results(self):
        docs = [_make_doc("foo", "bar baz")]
        retriever = HybridRetriever(docs, embedding_weight=0.0)
        results = retriever.query("bar", k=1)
        assert len(results) == 1
        assert isinstance(results[0], QueryResult)

    def test_type_filter_applied(self):
        d1 = Document(
            path="/a.md",
            source="brain",
            title="a",
            frontmatter={"name": "a", "description": "feedback memory", "type": "feedback"},
            body="",
            text="a feedback memory",
        )
        d2 = Document(
            path="/b.md",
            source="brain",
            title="b",
            frontmatter={"name": "b", "description": "reference memory", "type": "reference"},
            body="",
            text="b reference memory",
        )
        retriever = HybridRetriever([d1, d2], embedding_weight=0.0)
        results = retriever.query("memory", k=5, type_filter="feedback")
        assert len(results) == 1
        assert results[0].document.frontmatter["name"] == "a"

    def test_source_filter_applied(self):
        d1 = _make_doc("brain-doc", "brain content", source="brain")
        d2 = _make_doc("vault-doc", "vault content", source="vault")
        retriever = HybridRetriever([d1, d2], embedding_weight=0.0)
        results = retriever.query("content", k=5, source_filter="brain")
        assert all(r.document.source == "brain" for r in results)
