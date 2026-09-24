"""Tests for needs_review ranking policy (recall.core.apply_review_policy)."""

from __future__ import annotations

from recall.core import Document, QueryResult, _is_needs_review, apply_review_policy


def _doc(path: str, needs_review=None) -> Document:
    fm = {}
    if needs_review is not None:
        fm["needs_review"] = needs_review
    return Document(path=path, source="brain", title=path, frontmatter=fm, body="", text="")


def _qr(path: str, score: float, needs_review=None) -> QueryResult:
    return QueryResult(document=_doc(path, needs_review), score=score)


class TestIsNeedsReview:
    def test_bool_true(self):
        assert _is_needs_review(_doc("a", True))

    def test_bool_false(self):
        assert not _is_needs_review(_doc("a", False))

    def test_string_forms(self):
        assert _is_needs_review(_doc("a", "true"))
        assert _is_needs_review(_doc("a", "YES"))
        assert _is_needs_review(_doc("a", "1"))
        assert not _is_needs_review(_doc("a", "false"))
        assert not _is_needs_review(_doc("a", "maybe"))

    def test_absent(self):
        assert not _is_needs_review(_doc("a"))

    def test_raw_fallback_when_frontmatter_empty(self, tmp_path):
        # Simulates an indexed digest whose YAML failed to parse (empty
        # frontmatter), but whose raw file carries the flag. The fallback
        # must still detect it so the demotion doesn't miss broken digests.
        f = tmp_path / "broken.md"
        f.write_text(
            "---\noutcome: Scope negotiated: 502 deferred\nneeds_review: true\n---\nbody\n",
            encoding="utf-8",
        )
        doc = Document(path=str(f), source="brain", title="d", frontmatter={}, body="", text="")
        assert _is_needs_review(doc)

    def test_raw_fallback_negative_when_flag_absent(self, tmp_path):
        f = tmp_path / "clean.md"
        f.write_text("---\nname: a\n---\nbody\n", encoding="utf-8")
        doc = Document(path=str(f), source="brain", title="d", frontmatter={}, body="", text="")
        assert not _is_needs_review(doc)

    def test_raw_fallback_ignores_body_occurrence(self, tmp_path):
        # Empty parsed frontmatter, but the BODY (after the closer) mentions
        # "needs_review: true" — must NOT be treated as flagged.
        f = tmp_path / "broken.md"
        f.write_text(
            "---\noutcome: Scope: broke yaml\n---\n"
            "discussion of needs_review: true as a concept\n",
            encoding="utf-8",
        )
        doc = Document(path=str(f), source="brain", title="d", frontmatter={}, body="", text="")
        assert not _is_needs_review(doc)

    def test_raw_fallback_no_frontmatter_block(self, tmp_path):
        # A file with no frontmatter block at all that mentions the phrase
        # in prose must not be flagged.
        f = tmp_path / "plain.md"
        f.write_text("# notes\nneeds_review: true (just text)\n", encoding="utf-8")
        doc = Document(path=str(f), source="brain", title="d", frontmatter={}, body="", text="")
        assert not _is_needs_review(doc)

    def test_no_fallback_when_frontmatter_present(self, tmp_path):
        # A well-formed doc whose frontmatter simply lacks the flag must NOT
        # trigger a file read / fallback (frontmatter is non-empty).
        f = tmp_path / "present.md"
        f.write_text("needs_review: true\n", encoding="utf-8")  # would match if read
        doc = Document(path=str(f), source="brain", title="d",
                       frontmatter={"name": "a"}, body="", text="")
        assert not _is_needs_review(doc)


class TestApplyReviewPolicy:
    def test_ignore_is_noop(self):
        results = [_qr("a", 0.9, True), _qr("b", 0.5)]
        assert apply_review_policy(results, "ignore", 0.5) is results

    def test_exclude_drops_flagged(self):
        results = [_qr("stale", 0.9, True), _qr("fresh", 0.5)]
        out = apply_review_policy(results, "exclude", 0.5)
        assert [r.document.path for r in out] == ["fresh"]

    def test_demote_penalizes_and_resorts(self):
        # Stale doc scores higher (0.9) but should sink below fresh (0.6)
        # after a 0.5 penalty (0.9*0.5 = 0.45 < 0.6).
        results = [_qr("stale", 0.9, True), _qr("fresh", 0.6)]
        out = apply_review_policy(results, "demote", 0.5)
        assert [r.document.path for r in out] == ["fresh", "stale"]
        assert out[1].score == 0.9 * 0.5

    def test_demote_keeps_flagged_if_still_top(self):
        # Stale 0.9*0.5=0.45 still beats fresh 0.2 → stays first.
        results = [_qr("stale", 0.9, True), _qr("fresh", 0.2)]
        out = apply_review_policy(results, "demote", 0.5)
        assert out[0].document.path == "stale"

    def test_demote_penalty_zero_sinks_to_bottom(self):
        results = [_qr("stale", 0.99, True), _qr("fresh", 0.01)]
        out = apply_review_policy(results, "demote", 0.0)
        assert [r.document.path for r in out] == ["fresh", "stale"]

    def test_empty_input(self):
        assert apply_review_policy([], "demote", 0.5) == []

    def test_no_flagged_docs_unchanged_order(self):
        results = [_qr("a", 0.9), _qr("b", 0.5)]
        out = apply_review_policy(results, "demote", 0.5)
        assert [r.document.path for r in out] == ["a", "b"]


# ---------- staged remember lessons (trust/security workstream) ----------


def _staged_remember_qr(path: str, score: float) -> QueryResult:
    """A lesson exactly as `recall remember` (default, unreviewed) writes
    it: needs_review + review_reason=unreviewed-remember + source
    recall-remember in frontmatter."""
    doc = Document(
        path=path,
        source="brain",
        title=path,
        frontmatter={
            "needs_review": True,
            "review_reason": "unreviewed-remember",
            "source": "recall-remember",
        },
        body="",
        text="",
    )
    return QueryResult(document=doc, score=score)


class TestStagedRememberLessons:
    def test_staged_remember_lesson_demoted_or_excluded(self):
        """An unreviewed `recall remember` write must rank below an
        otherwise-identical unflagged doc under the demote policy, and
        disappear entirely under exclude. This is the retrieval half of
        the review gate: agent-written lessons cannot outrank reviewed
        memory until a human accepts them via `recall pending --review`."""
        staged = _staged_remember_qr("staged-remember", 0.9)
        durable = _qr("durable", 0.6)

        # demote: staged sinks below the unflagged doc despite the
        # higher raw score (0.9 * 0.5 = 0.45 < 0.6).
        out = apply_review_policy([staged, durable], "demote", 0.5)
        assert [r.document.path for r in out] == ["durable", "staged-remember"]

        # exclude: staged is dropped entirely.
        out = apply_review_policy([staged, durable], "exclude", 0.5)
        assert [r.document.path for r in out] == ["durable"]


# ---------- expanded + reranked queries keep the policy (Codex seam fix) ----------


class _PolicyStubRetriever:
    """Minimal HybridRetriever stand-in: scripted results plus the
    needs_review knobs `cli._expanded_query` reads back off the retriever."""

    def __init__(self, results, policy: str, penalty: float = 0.5):
        self._results = list(results)
        self._needs_review_policy = policy
        self._needs_review_penalty = penalty

    def query(self, query, k=5, type_filter=None, source_filter=None):
        return list(self._results)[:k]


class TestExpandedRerankReviewPolicy:
    """`--expand` with a cross-encoder rerank must re-apply the
    needs_review policy AFTER the fused-union rerank. The cross-encoder
    replaces the per-variant scores (where the demotion lived) with raw
    relevance, so without the re-application a flagged memory the encoder
    likes floats back above fresh ones in the final ordering."""

    def _run_expanded_rerank(self, policy: str):
        from unittest.mock import MagicMock, patch

        from recall import cli as cli_mod

        # Flagged doc ranked ABOVE the unflagged one in every variant, and
        # the cross-encoder scores them as EQUAL: any final ordering change
        # can only come from the review policy.
        flagged = _qr("flagged", 0.9, True)
        fresh = _qr("fresh", 0.9)
        retriever = _PolicyStubRetriever([flagged, fresh], policy)

        fake_encoder = MagicMock()
        fake_encoder.rerank.side_effect = lambda q, texts: [1.0 for _ in texts]

        with patch(
            "recall.expand.expand_query",
            side_effect=lambda q, n=3, provider=None: [q, f"alt-of-{q}"],
        ), patch(
            "recall.qdrant_backend._get_cross_encoder", return_value=fake_encoder
        ):
            return cli_mod._expanded_query(
                retriever,
                "the question",
                k=5,
                expand_n=1,
                strategy="ranked",
                rerank_model="any/model",
            )

    def test_demote_keeps_flagged_below_unflagged_equal(self):
        results = self._run_expanded_rerank("demote")
        paths = [r.document.path for r in results]
        assert paths == ["fresh", "flagged"], (
            f"flagged doc must sink below the unflagged equal after the "
            f"fused rerank, got {paths}"
        )
        # The demotion is visible in the score too (1.0 * 0.5 penalty).
        assert results[1].score < results[0].score

    def test_exclude_drops_flagged_from_reranked_union(self):
        results = self._run_expanded_rerank("exclude")
        paths = [r.document.path for r in results]
        assert paths == ["fresh"], (
            f"flagged doc must be excluded from the reranked union, got {paths}"
        )


# ---------- superseded / temporal ranking policy (Phase 2) -----------------


def _tdoc(path: str, **fm) -> Document:
    return Document(path=path, source="brain", title=path, frontmatter=fm, body="", text="")


def _tqr(path: str, score: float, **fm) -> QueryResult:
    return QueryResult(document=_tdoc(path, **fm), score=score)


class TestIsSuperseded:
    def test_status_superseded(self):
        from recall.core import _is_superseded
        assert _is_superseded(_tdoc("a", status="superseded"))

    def test_superseded_by_pointer(self):
        from recall.core import _is_superseded
        assert _is_superseded(_tdoc("a", superseded_by="lesson_new"))

    def test_stance_alias(self):
        from recall.core import _is_superseded
        assert _is_superseded(_tdoc("a", stance="superseded"))

    def test_claim_stale_type(self):
        from recall.core import _is_superseded
        assert _is_superseded(_tdoc("a", type="claim-stale"))

    def test_missing_frontmatter_is_not_superseded(self):
        from recall.core import _is_superseded
        assert not _is_superseded(_tdoc("a"))
        assert not _is_superseded(Document(path="b", source="brain", title="b",
                                           frontmatter=None, body="", text=""))

    def test_current_status_is_not_superseded(self):
        from recall.core import _is_superseded
        assert not _is_superseded(_tdoc("a", status="current"))
        assert not _is_superseded(_tdoc("a", status="accepted"))


class TestApplyTemporalPolicy:
    def test_demote_puts_current_ahead_of_superseded(self):
        from recall.core import apply_temporal_policy
        old = _tqr("old", 0.9, status="superseded", superseded_by="new")
        new = _tqr("new", 0.6, status="current")
        out = apply_temporal_policy([old, new], "demote", 0.5)
        # 0.9 * 0.5 = 0.45 < 0.6 — the current doc wins despite losing RRF.
        assert [r.document.path for r in out] == ["new", "old"]

    def test_exclude_drops_superseded(self):
        from recall.core import apply_temporal_policy
        old = _tqr("old", 0.9, status="superseded")
        new = _tqr("new", 0.6, status="current")
        out = apply_temporal_policy([old, new], "exclude", 0.5)
        assert [r.document.path for r in out] == ["new"]

    def test_ignore_is_noop(self):
        from recall.core import apply_temporal_policy
        old = _tqr("old", 0.9, status="superseded")
        new = _tqr("new", 0.6)
        out = apply_temporal_policy([old, new], "ignore", 0.5)
        assert [r.document.path for r in out] == ["old", "new"]

    def test_missing_frontmatter_is_not_demoted(self):
        """Back-compat: pre-Phase-2 docs carry no temporal fields and must
        keep their full score."""
        from recall.core import apply_temporal_policy
        plain = _tqr("plain", 0.9)
        new = _tqr("new", 0.6, status="current")
        out = apply_temporal_policy([plain, new], "demote", 0.5)
        assert [r.document.path for r in out] == ["plain", "new"]
        assert out[0].score == 0.9

    def test_claim_stale_type_is_treated_as_superseded(self):
        from recall.core import apply_temporal_policy
        stale = _tqr("stale", 0.9, type="claim-stale")
        current = _tqr("current", 0.6, type="claim-current")
        out = apply_temporal_policy([stale, current], "demote", 0.5)
        assert [r.document.path for r in out] == ["current", "stale"]

    def test_demote_applies_to_rerank_score_sign_safely(self):
        from recall.core import apply_temporal_policy
        old = QueryResult(document=_tdoc("old", status="superseded"),
                          score=0.9, rerank_score=-2.0)
        new = QueryResult(document=_tdoc("new", status="current"),
                          score=0.6, rerank_score=-3.0)
        out = apply_temporal_policy([old, new], "demote", 0.5)
        # Negative logits: dividing by the penalty moves -2.0 → -4.0 (DOWN).
        assert [r.document.path for r in out] == ["new", "old"]
        assert out[1].rerank_score == -4.0

    def test_tie_break_prefers_later_valid_from(self):
        from recall.core import apply_temporal_policy
        older = _tqr("older", 0.8, status="current", valid_from="2025-01-01T00:00:00Z")
        newer = _tqr("newer", 0.8, status="current", valid_from="2026-06-01T00:00:00Z")
        out = apply_temporal_policy([older, newer], "demote", 0.5)
        assert [r.document.path for r in out] == ["newer", "older"]

    def test_empty_and_unknown_policy(self):
        from recall.core import apply_temporal_policy
        assert apply_temporal_policy([], "demote", 0.5) == []
        qr = _tqr("a", 0.9, status="superseded")
        assert apply_temporal_policy([qr], "bogus", 0.5) == [qr]


class TestExpandedRerankTemporalPolicy:
    """`--expand` + cross-encoder rerank must re-apply the temporal policy
    after the fused-union rerank, same seam as the needs_review policy."""

    def _run_expanded_rerank(self, policy: str):
        from unittest.mock import MagicMock, patch

        from recall import cli as cli_mod

        stale = _tqr("stale", 0.9, status="superseded")
        fresh = _tqr("fresh", 0.9, status="current")
        retriever = _PolicyStubRetriever([stale, fresh], "ignore")
        retriever._superseded_policy = policy
        retriever._superseded_penalty = 0.5

        fake_encoder = MagicMock()
        fake_encoder.rerank.side_effect = lambda q, texts: [1.0 for _ in texts]

        with patch(
            "recall.expand.expand_query",
            side_effect=lambda q, n=3, provider=None: [q, f"alt-of-{q}"],
        ), patch(
            "recall.qdrant_backend._get_cross_encoder", return_value=fake_encoder
        ):
            return cli_mod._expanded_query(
                retriever,
                "the question",
                k=5,
                expand_n=1,
                strategy="ranked",
                rerank_model="any/model",
            )

    def test_expanded_rerank_reapplies_temporal_policy(self):
        results = self._run_expanded_rerank("demote")
        paths = [r.document.path for r in results]
        assert paths == ["fresh", "stale"], (
            f"superseded doc must sink below the current equal after the "
            f"fused rerank, got {paths}"
        )

    def test_expanded_rerank_exclude_drops_superseded(self):
        results = self._run_expanded_rerank("exclude")
        assert [r.document.path for r in results] == ["fresh"]
