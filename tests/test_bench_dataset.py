"""Hermetic guards for the recall A/B benchmark dataset and harness.

These do NOT run retrieval (no embedding model). They check that the shipped
labeled set is internally consistent and that the harness imports and scores a
trivial in-memory result correctly, so a broken dataset or scoring bug is
caught in CI without a model download. The full retrieval run lives behind
`make bench` / the embeddings marker.
"""
from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET = REPO_ROOT / "eval" / "bench_dataset.json"


def _data() -> dict:
    return json.loads(DATASET.read_text(encoding="utf-8"))


def test_dataset_present_and_shaped():
    data = _data()
    assert data["corpus"], "corpus must be non-empty"
    assert data["questions"], "questions must be non-empty"


def test_every_support_resolves_to_a_corpus_doc():
    data = _data()
    slugs = {d["slug"] for d in data["corpus"]}
    for q in data["questions"]:
        for s in q.get("supports", []):
            assert s in slugs, f"question {q['q']!r} supports unknown slug {s!r}"


def test_every_question_has_supports_and_answer_substring():
    data = _data()
    for q in data["questions"]:
        assert q.get("supports"), f"question {q['q']!r} has no supports"
        assert q.get("answer_substring"), f"question {q['q']!r} has no answer_substring"


def test_corpus_slugs_unique():
    data = _data()
    slugs = [d["slug"] for d in data["corpus"]]
    assert len(slugs) == len(set(slugs)), "duplicate corpus slugs"


def test_corpus_is_large_enough_to_be_nontrivial():
    # A meaningful retrieval score needs a corpus big enough that the top hit
    # is not guaranteed. (Vocabulary-overlapping near-neighbors, not the count
    # of unreferenced docs, are what create the confusability; this is just a
    # floor so the set cannot shrink to a trivial lookup.)
    data = _data()
    assert len(data["corpus"]) >= 12, "benchmark corpus too small to be meaningful"
    assert len(data["questions"]) >= 12, "benchmark needs enough questions to be stable"


def test_harness_scores_a_synthetic_result_without_a_model(monkeypatch):
    # Exercise run_benchmark's scoring path with a fake retriever so the
    # metric math is covered hermetically (no embeddings).
    import eval.bench_recall_ab as bench
    from recall.core import Document, QueryResult

    tiny = {
        "corpus": [
            {"slug": "a", "title": "A", "body": "the answer is alpha"},
            {"slug": "b", "title": "B", "body": "unrelated beta"},
        ],
        "questions": [
            {"q": "where is alpha", "supports": ["a"], "answer_substring": "alpha"},
        ],
    }

    class _FakeRetriever:
        def __init__(self, *a, **k):
            pass

        def query(self, q, k=5):
            # Rank the supporting doc first.
            docs = [
                Document(path="a", source="bench", title="A", frontmatter={},
                         body="the answer is alpha", text="A the answer is alpha"),
                Document(path="b", source="bench", title="B", frontmatter={},
                         body="unrelated beta", text="B unrelated beta"),
            ]
            return [QueryResult(document=d, score=1.0 - i) for i, d in enumerate(docs)]

    # run_benchmark does a lazy `from recall.core import HybridRetriever`, so
    # patch it at the source module.
    import recall.core
    monkeypatch.setattr(recall.core, "HybridRetriever", _FakeRetriever)
    result = bench.run_benchmark(tiny, k=5)
    assert result["recall@1"] == 1.0
    assert result["mrr"] == 1.0
    assert result["answer_coverage@5"] == 1.0
    assert result["n_questions"] == 1
