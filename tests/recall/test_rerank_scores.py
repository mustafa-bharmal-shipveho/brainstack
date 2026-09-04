"""Red-phase tests (S4): RRF score and rerank score travel side by side.

Planned contract (not implemented yet):

  - `QueryResult` gains `rerank_score: float | None = None`, declared AFTER
    `score` so `QueryResult(doc, 0.4, 0.9)` stays valid positionally. Today
    `query_hybrid_rerank` REPLACES `score` with the cross-encoder score; the
    S4 relevance gate needs both — the cheap RRF `score` as a pre-filter and
    the cross-encoder `rerank_score` as the decision.
  - `apply_review_policy(..., "demote", penalty)` scales BOTH `score` and
    `rerank_score` (when present) and re-sorts by the rerank score when the
    results carry one, falling back to `score` when they don't.
  - `serialize_results` ALWAYS emits a `rerank_score` key: `null` when the
    result has none, a 6-dp rounded float otherwise. The daemon wire format
    and the CLI JSON are pinned to that shape.
  - `qdrant_backend.dense_fallback_active() -> bool` exposes the
    once-per-process sparse-fallback flag so the hook can report
    `x_degraded` honestly.

Hermetic: no embedder, no cross-encoder, no Qdrant client, no disk reads.
Every synthetic Document carries non-empty frontmatter so
`_is_needs_review` never falls back to reading the (nonexistent) file.
"""

from __future__ import annotations

import pytest

from recall import qdrant_backend
from recall.core import Document, QueryResult, apply_review_policy
from recall.serialize import serialize_results


def _doc(name: str, *, needs_review: bool | None = None) -> Document:
    fm: dict = {
        "name": name,
        "description": f"synthetic description for {name}",
        "type": "reference",
    }
    if needs_review is not None:
        fm["needs_review"] = needs_review
    return Document(
        path=f"/synth/brain/{name}.md",
        source="brain",
        title=name,
        frontmatter=fm,
        body=f"body of {name}",
        text=f"{name} body of {name}",
    )


def _qr(
    name: str,
    score: float,
    rerank_score: float | None = None,
    *,
    needs_review: bool | None = None,
) -> QueryResult:
    return QueryResult(
        document=_doc(name, needs_review=needs_review),
        score=score,
        rerank_score=rerank_score,
    )


def _names(results: list[QueryResult]) -> list[str]:
    return [r.document.frontmatter["name"] for r in results]


class TestQueryResultRerankScore:
    def test_default_rerank_score_is_none(self):
        # Every existing caller constructs QueryResult(document=, score=)
        # only; they must keep working and report "no rerank score".
        r = QueryResult(document=_doc("a"), score=0.42)
        assert r.rerank_score is None

    def test_rerank_score_is_third_positional_field(self):
        # Field ORDER is part of the contract: qdrant_backend builds these
        # in tight loops and the daemon wire projection reads them by name,
        # but tests and adapters construct them positionally.
        r = QueryResult(_doc("a"), 0.4, 0.9)
        assert r.score == pytest.approx(0.4)
        assert r.rerank_score == pytest.approx(0.9)

    def test_rerank_score_accepts_none_explicitly(self):
        r = QueryResult(document=_doc("a"), score=0.4, rerank_score=None)
        assert r.rerank_score is None


class TestReviewPolicySortsByRerankScore:
    def test_sorts_by_rerank_score_desc_when_present(self):
        # RRF order (c, b, a) disagrees with rerank order (a, b, c). The
        # reranker wins: it is the more accurate signal and the one the gate
        # thresholds on.
        results = [
            _qr("c", score=0.9, rerank_score=0.1),
            _qr("b", score=0.5, rerank_score=0.5),
            _qr("a", score=0.1, rerank_score=0.9),
        ]
        out = apply_review_policy(results, "demote", 0.5)
        assert _names(out) == ["a", "b", "c"]

    def test_falls_back_to_rrf_score_when_no_rerank_scores(self):
        # In-process / reranker="none" path: today's behaviour unchanged.
        results = [
            _qr("low", score=0.1),
            _qr("high", score=0.9),
            _qr("mid", score=0.5),
        ]
        out = apply_review_policy(results, "demote", 0.5)
        assert _names(out) == ["high", "mid", "low"]

    def test_ties_break_on_document_path(self):
        results = [
            _qr("zulu", score=0.5, rerank_score=0.7),
            _qr("alpha", score=0.5, rerank_score=0.7),
        ]
        out = apply_review_policy(results, "demote", 0.5)
        assert _names(out) == ["alpha", "zulu"]


class TestReviewPolicyDemoteScalesBothScores:
    def test_demote_scales_score_and_rerank_score(self):
        flagged = _qr("stale", score=0.8, rerank_score=0.9, needs_review=True)
        fresh = _qr("fresh", score=0.4, rerank_score=0.5)
        out = apply_review_policy([flagged, fresh], "demote", 0.5)

        demoted = next(r for r in out if r.document.frontmatter["name"] == "stale")
        kept = next(r for r in out if r.document.frontmatter["name"] == "fresh")

        assert demoted.score == pytest.approx(0.4)
        assert demoted.rerank_score == pytest.approx(0.45)
        # Untouched result keeps both scores exactly.
        assert kept.score == pytest.approx(0.4)
        assert kept.rerank_score == pytest.approx(0.5)
        # 0.45 < 0.5, so the penalty actually reorders the pair.
        assert _names(out) == ["fresh", "stale"]

    def test_demote_leaves_missing_rerank_score_as_none(self):
        flagged = _qr("stale", score=0.8, needs_review=True)
        out = apply_review_policy([flagged], "demote", 0.5)
        assert out[0].score == pytest.approx(0.4)
        assert out[0].rerank_score is None

    def test_exclude_policy_drops_flagged_and_preserves_rerank_score(self):
        flagged = _qr("stale", score=0.8, rerank_score=0.9, needs_review=True)
        fresh = _qr("fresh", score=0.4, rerank_score=0.5)
        out = apply_review_policy([flagged, fresh], "exclude", 0.5)
        assert _names(out) == ["fresh"]
        assert out[0].rerank_score == pytest.approx(0.5)

    def test_ignore_policy_returns_input_unchanged(self):
        results = [
            _qr("c", score=0.9, rerank_score=0.1),
            _qr("a", score=0.1, rerank_score=0.9),
        ]
        out = apply_review_policy(results, "ignore", 0.5)
        assert out == results


class TestSerializeRerankScore:
    def test_rerank_score_key_is_null_when_absent(self):
        out = serialize_results([_qr("a", score=0.5)])
        assert len(out) == 1
        assert "rerank_score" in out[0], "key must always be present"
        assert out[0]["rerank_score"] is None

    def test_rerank_score_rounded_to_six_decimals(self):
        out = serialize_results([_qr("a", score=0.5, rerank_score=0.1234567891)])
        assert out[0]["rerank_score"] == pytest.approx(0.123457)

    def test_rrf_score_still_serialized_alongside(self):
        out = serialize_results([_qr("a", score=0.0328123456, rerank_score=0.9)])
        assert out[0]["score"] == pytest.approx(0.032812)
        assert out[0]["rerank_score"] == pytest.approx(0.9)
        # Existing keys are untouched.
        assert out[0]["path"] == "/synth/brain/a.md"
        assert out[0]["source"] == "brain"

    def test_zero_rerank_score_is_not_confused_with_none(self):
        out = serialize_results([_qr("a", score=0.5, rerank_score=0.0)])
        assert out[0]["rerank_score"] == 0.0
        assert out[0]["rerank_score"] is not None


class TestRerankNCapsTotalPairs:
    """`rerank_n` is a budget, not a per-collection floor.

    Reranking used to run once PER COLLECTION on a pool of
    `fetch_n = max(2*k, rerank_n, k+10)`. With two sources and k=10 that is
    40 cross-encoder pairs for a nominal `rerank_n=10` — 4x the configured
    budget, and the dominant cost in the hook's latency budget. The pool is
    merged and RRF-ordered first; the cross-encoder then sees at most
    `rerank_n` pairs in total.

    Hermetic: the Qdrant client, collection setup, the hybrid query and the
    cross-encoder are all faked, so no embedder, store or disk is touched.
    """

    K = 3
    RERANK_N = 6

    @pytest.fixture
    def counting_encoder(self, monkeypatch):
        seen: dict = {"calls": 0, "pairs": 0, "fetch_n": []}

        class _Encoder:
            def rerank(self, query, texts):
                seen["calls"] += 1
                seen["pairs"] += len(texts)
                # Score by position so the rerank order REVERSES the RRF
                # order: if the cap were applied after reranking rather than
                # before, a different document would come out on top.
                return [float(i) for i in range(len(texts))]

        def _fake_query_hybrid(client, collection, query, k, **kwargs):
            seen["fetch_n"].append(k)
            # Two collections interleave on score: a00 1.00, b00 0.995,
            # a01 0.99, b01 0.985, ...
            offset = 0.0 if collection == "a" else 0.005
            return [
                QueryResult(
                    document=_doc(f"{collection}{i:02d}"),
                    score=1.0 - offset - i * 0.01,
                )
                for i in range(20)
            ]

        monkeypatch.setattr(qdrant_backend, "_qdrant_client_singleton", lambda *a, **k: object())
        monkeypatch.setattr(qdrant_backend, "ensure_collection", lambda *a, **k: None)
        monkeypatch.setattr(qdrant_backend, "query_hybrid", _fake_query_hybrid)
        monkeypatch.setattr(qdrant_backend, "_get_cross_encoder", lambda model: _Encoder())
        return seen

    def _retriever(self):
        from recall.core import HybridRetriever

        return HybridRetriever(
            collections=["a", "b"],
            reranker="cross_encoder",
            rerank_n=self.RERANK_N,
            # The default policy, which is what inflated `fetch_n`.
            needs_review_policy="demote",
        )

    def test_reranks_at_most_rerank_n_pairs_across_all_collections(
        self, counting_encoder
    ):
        self._retriever().query("anything", k=self.K)
        assert counting_encoder["pairs"] == self.RERANK_N, (
            f"reranked {counting_encoder['pairs']} pairs for rerank_n="
            f"{self.RERANK_N}; the cap must span collections, not repeat per one"
        )

    def test_reranks_once_not_once_per_collection(self, counting_encoder):
        self._retriever().query("anything", k=self.K)
        assert counting_encoder["calls"] == 1

    def test_returns_k_results_ordered_by_rerank_score(self, counting_encoder):
        results = self._retriever().query("anything", k=self.K)

        assert len(results) == self.K
        # Top-6 by RRF is a00, b00, a01, b01, a02, b02; the fake encoder
        # scores by position, so b02 (last in, highest score) wins.
        assert _names(results) == ["b02", "a02", "b01"]
        scores = [r.rerank_score for r in results]
        assert all(s is not None for s in scores)
        assert scores == sorted(scores, reverse=True)

    def test_rrf_score_survives_the_rerank(self, counting_encoder):
        results = self._retriever().query("anything", k=self.K)
        assert all(0.0 <= r.score <= 1.0 for r in results)

    def test_rrf_leg_still_over_fetches_for_the_review_policy(self, counting_encoder):
        # The deeper candidate pull is what lets a demoted needs_review memory
        # be replaced by a fresh one below it. Capping the RERANK budget must
        # not shrink the RRF fetch.
        self._retriever().query("anything", k=self.K)
        assert counting_encoder["fetch_n"], "query_hybrid was never called"
        assert all(n >= 2 * self.K for n in counting_encoder["fetch_n"])


class TestDenseFallbackActive:
    """`dense_fallback_active()` mirrors the once-per-process warn flag.

    The flag is module-global, so it is cleared before AND after each test
    here — otherwise a set flag leaks into every later test in the session
    (and a fallback triggered by an earlier test would leak into this one).
    """

    @pytest.fixture(autouse=True)
    def _reset_flag(self):
        qdrant_backend._SPARSE_FALLBACK_WARN_ONCE.clear()
        yield
        qdrant_backend._SPARSE_FALLBACK_WARN_ONCE.clear()

    def test_false_by_default(self):
        assert qdrant_backend.dense_fallback_active() is False

    def test_true_after_sparse_fallback_warned(self):
        qdrant_backend._SPARSE_FALLBACK_WARN_ONCE.set()
        assert qdrant_backend.dense_fallback_active() is True

    def test_reset_hook_clears_it_again(self):
        qdrant_backend._SPARSE_FALLBACK_WARN_ONCE.set()
        qdrant_backend._reset_sparse_fallback_warning_for_tests()
        assert qdrant_backend.dense_fallback_active() is False


class TestDemoteIsSignSafeForLogits:
    """`demote` must move a flagged doc DOWN whatever the score's sign.

    Cross-encoder outputs are raw logits, and in practice most of them are
    NEGATIVE. Multiplying a negative score by a penalty < 1 moves it TOWARD
    zero, i.e. UP under `_rank_key` — the exact opposite of a demotion, and
    it also lifts the doc over `auto_recall_min_rerank`. The penalty has to
    be applied sign-safely: multiply when positive, divide when negative.

    The RRF `score` is a fused rank reciprocal and always positive, so it
    keeps the plain `score * penalty` form.
    """

    def test_negative_rerank_score_is_pushed_further_negative(self):
        flagged = _qr("stale", score=0.8, rerank_score=-1.0, needs_review=True)
        out = apply_review_policy([flagged], "demote", 0.5)
        # -1.0 * 0.5 == -0.5 would be a PROMOTION. -1.0 / 0.5 == -2.0.
        assert out[0].rerank_score == pytest.approx(-2.0)

    def test_demoted_negative_doc_ranks_below_an_undemoted_worse_one(self):
        # Pre-penalty the flagged doc is the better match (-1.0 > -1.5).
        # After a 0.5 penalty it must fall behind the fresh one.
        flagged = _qr("stale", score=0.8, rerank_score=-1.0, needs_review=True)
        fresh = _qr("fresh", score=0.4, rerank_score=-1.5)
        out = apply_review_policy([flagged, fresh], "demote", 0.5)
        assert _names(out) == ["fresh", "stale"]

    def test_positive_rerank_score_still_scaled_down(self):
        flagged = _qr("stale", score=0.8, rerank_score=1.0, needs_review=True)
        out = apply_review_policy([flagged], "demote", 0.5)
        assert out[0].rerank_score == pytest.approx(0.5)

    def test_zero_rerank_score_stays_zero(self):
        # 0.0 keeps its sign-free identity; guard against a boundary rewrite
        # that flips it or divides it into something else.
        flagged = _qr("stale", score=0.8, rerank_score=0.0, needs_review=True)
        out = apply_review_policy([flagged], "demote", 0.5)
        assert out[0].rerank_score == pytest.approx(0.0)

    def test_rrf_score_keeps_plain_multiplication_when_rerank_is_negative(self):
        # RRF fusion scores are always positive, so multiplying is already
        # sign-safe there; the negative-logit fix must not touch it.
        flagged = _qr("stale", score=0.8, rerank_score=-1.0, needs_review=True)
        out = apply_review_policy([flagged], "demote", 0.5)
        assert out[0].score == pytest.approx(0.4)

    def test_unflagged_negative_scores_are_untouched(self):
        fresh = _qr("fresh", score=0.4, rerank_score=-1.5)
        out = apply_review_policy([fresh], "demote", 0.5)
        assert out[0].rerank_score == pytest.approx(-1.5)
        assert out[0].score == pytest.approx(0.4)

    def test_negative_demotion_survives_a_realistic_gate_threshold(self):
        # With auto_recall_min_rerank calibrated at -1.2, the buggy
        # multiplication (-1.0 -> -0.5) sails through the gate; the sign-safe
        # form (-1.0 -> -2.0) is correctly filtered out.
        min_rerank = -1.2
        flagged = _qr("stale", score=0.8, rerank_score=-1.0, needs_review=True)
        out = apply_review_policy([flagged], "demote", 0.5)
        assert out[0].rerank_score < min_rerank


class TestSingleCandidateIsStillScored:
    """One surviving candidate must still get a `rerank_score`.

    The relevance gate treats `rerank_score is None` as "pass" (there is no
    score to judge). Short-circuiting the single-candidate case therefore let
    exactly one off-topic memory bypass a configured `auto_recall_min_rerank`
    entirely — and the narrowest pool is where the gate matters most.

    The empty pool still short-circuits: there is nothing to score.
    """

    @pytest.fixture
    def counting_encoder(self, monkeypatch):
        seen: dict = {"calls": 0, "pairs": 0}

        class _Encoder:
            def rerank(self, query, texts):
                seen["calls"] += 1
                seen["pairs"] += len(texts)
                return [-1.75 for _ in texts]

        monkeypatch.setattr(
            qdrant_backend, "_get_cross_encoder", lambda model: _Encoder()
        )
        return seen

    def test_single_candidate_is_scored_with_exactly_one_pair(self, counting_encoder):
        qdrant_backend.rerank_results("q", [_qr("solo", score=0.5)], limit=10)
        assert counting_encoder["calls"] == 1
        assert counting_encoder["pairs"] == 1

    def test_single_candidate_gets_a_float_rerank_score(self, counting_encoder):
        out = qdrant_backend.rerank_results("q", [_qr("solo", score=0.5)], limit=10)
        assert len(out) == 1
        assert isinstance(out[0].rerank_score, float)
        assert out[0].rerank_score == pytest.approx(-1.75)

    def test_single_candidate_keeps_its_rrf_score_and_document(self, counting_encoder):
        out = qdrant_backend.rerank_results("q", [_qr("solo", score=0.5)], limit=10)
        assert out[0].score == pytest.approx(0.5)
        assert out[0].document.path == "/synth/brain/solo.md"

    def test_limit_of_one_scores_the_single_kept_candidate(self, counting_encoder):
        # A limit that truncates the pool down to one is the same case.
        pool = [_qr("a", score=0.9), _qr("b", score=0.5)]
        out = qdrant_backend.rerank_results("q", pool, limit=1)
        assert counting_encoder["pairs"] == 1
        assert _names(out) == ["a"]
        assert out[0].rerank_score == pytest.approx(-1.75)

    def test_empty_candidates_still_short_circuit_without_the_model(
        self, counting_encoder
    ):
        assert qdrant_backend.rerank_results("q", [], limit=10) == []
        assert counting_encoder["calls"] == 0

    def test_non_positive_limit_still_short_circuits(self, counting_encoder):
        assert qdrant_backend.rerank_results("q", [_qr("a", score=0.5)], limit=0) == []
        assert counting_encoder["calls"] == 0
