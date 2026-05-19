"""Unit tests for RRF fusion (recall/fusion.py).

Fast, no fastembed/Qdrant dependency. Tests document-identity merging,
rank-position scoring, and ordering correctness.
"""
from __future__ import annotations

from recall.core import Document, QueryResult
from recall.fusion import RRF_K, rrf_merge


def _doc(path: str) -> Document:
    return Document(
        path=path,
        source="test",
        title=path,
        frontmatter={},
        body=f"body of {path}",
        text=f"text of {path}",
    )


def _qr(path: str, score: float = 0.0) -> QueryResult:
    return QueryResult(document=_doc(path), score=score)


class TestRRFMerge:
    def test_empty_input_returns_empty(self):
        assert rrf_merge([]) == []
        assert rrf_merge([[], [], []]) == []

    def test_single_list_preserves_order(self):
        out = rrf_merge([[_qr("a"), _qr("b"), _qr("c")]])
        assert [r.document.path for r in out] == ["a", "b", "c"]

    def test_doc_in_multiple_variants_wins(self):
        # `b` is rank 2 in both lists. `a` and `d` are rank 1 in their
        # respective lists. RRF sums: b gets 2/(60+2) ≈ 0.032, a/d each
        # get 1/(60+1) ≈ 0.016. b wins.
        out = rrf_merge([
            [_qr("a"), _qr("b"), _qr("c")],
            [_qr("d"), _qr("b"), _qr("e")],
        ])
        paths = [r.document.path for r in out]
        assert paths[0] == "b", f"expected b first, got {paths}"
        assert set(paths) == {"a", "b", "c", "d", "e"}, "all docs preserved"

    def test_rank_position_matters(self):
        # `b` is rank 1 in list2, rank 5 in list1. `a` is rank 1 in list1
        # only. b's score = 1/(60+5) + 1/(60+1) ≈ 0.0317. a's score =
        # 1/(60+1) ≈ 0.0164. b wins.
        out = rrf_merge([
            [_qr("a"), _qr("c"), _qr("d"), _qr("e"), _qr("b")],
            [_qr("b"), _qr("f")],
        ])
        paths = [r.document.path for r in out]
        assert paths[0] == "b", f"b should win on cross-variant boost; got {paths}"

    def test_rrf_k_constant(self):
        assert RRF_K == 60

    def test_returns_query_results_with_fused_score(self):
        out = rrf_merge([[_qr("a"), _qr("b")]])
        # Score for rank-1 in single list = 1/(60+1) = 0.01639...
        assert isinstance(out[0], QueryResult)
        assert abs(out[0].score - 1.0 / 61.0) < 1e-9
        assert abs(out[1].score - 1.0 / 62.0) < 1e-9

    def test_document_identity_by_path(self):
        # Two QueryResult instances pointing at the same path should be
        # treated as the same document and accumulate score.
        d1 = QueryResult(document=_doc("shared"), score=0.5)
        d2 = QueryResult(document=_doc("shared"), score=0.3)
        out = rrf_merge([[d1, _qr("a")], [d2, _qr("b")]])
        paths = [r.document.path for r in out]
        # shared appears in both → should be #1
        assert paths[0] == "shared"
        # And exactly one entry for "shared" (not two)
        assert paths.count("shared") == 1

    def test_custom_k_parameter(self):
        # With k=0, smaller ranks dominate even more.
        out_k60 = rrf_merge([[_qr("a"), _qr("b"), _qr("c")]])
        out_k0 = rrf_merge([[_qr("a"), _qr("b"), _qr("c")]], k=0)
        # Same ordering, different scores
        assert [r.document.path for r in out_k60] == ["a", "b", "c"]
        assert [r.document.path for r in out_k0] == ["a", "b", "c"]
        # k=0 yields 1.0 for rank 1 vs 0.0164 for k=60
        assert abs(out_k0[0].score - 1.0) < 1e-9
