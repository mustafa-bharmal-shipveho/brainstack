"""Recall public surface: Document, QueryResult, HybridRetriever facade.

HybridRetriever is the only retrieval entrypoint. It delegates dense+sparse
hybrid scoring to recall.qdrant_backend; this module holds only the public
dataclasses and a thin facade so cli.py / mcp_server.py / tests don't have
to know about Qdrant.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass(frozen=True)
class Document:
    path: str
    source: str
    title: str
    frontmatter: dict
    body: str
    text: str


@dataclass(frozen=True)
class QueryResult:
    document: Document
    score: float
    # S4: cross-encoder rerank score, travels alongside the cheap RRF
    # `score` rather than replacing it. None on any path that never loaded
    # a reranker (in-process auto-recall fallback, reranker="none").
    # Declared AFTER score so existing positional callers
    # (`QueryResult(doc, score)`) keep working unchanged.
    rerank_score: Optional[float] = None


_NEEDS_REVIEW_RAW_RE = re.compile(
    r"""(?mi)^needs_review[ \t]*:[ \t]*['"]?(true|yes|1)\b""")


def _is_needs_review(doc: Document) -> bool:
    """True if a document is flagged `needs_review` in its frontmatter.

    Accepts the YAML-truthy forms a human or tool might write: the boolean
    True, or the strings "true"/"yes"/"1" (case-insensitive).

    Fallback: if the parsed frontmatter is EMPTY (some real digests have
    malformed YAML — e.g. an unquoted ``outcome:`` containing a colon — so
    the indexed frontmatter parsed to ``{}``), scan the raw file for the
    flag directly. This costs one small read only for the rare
    empty-frontmatter doc in a result set, never for well-formed memories.
    """
    fm = doc.frontmatter or {}
    val = fm.get("needs_review")
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in {"true", "yes", "1"}
    if not fm and doc.path:
        try:
            with open(doc.path, encoding="utf-8") as fh:
                head = fh.read(8192)
        except (OSError, ValueError):
            return False
        # Scan ONLY the leading frontmatter block (between the first two
        # `---` delimiters), not the body — a body line that happens to read
        # "needs_review: true" must not trigger a demotion. The delimiters
        # are intact even when the YAML *content* between them is malformed
        # (the exact reason this fallback exists).
        if not head.startswith("---"):
            return False
        end = head.find("\n---", 3)
        block = head[:end] if end != -1 else head
        return bool(_NEEDS_REVIEW_RAW_RE.search(block))
    return False


def _rank_key(r: QueryResult) -> tuple[float, str]:
    """Sort key: best signal first, path as the deterministic tie-break.

    The cross-encoder score is the more accurate signal and the one the S4
    relevance gate thresholds on, so it wins whenever it is present. Results
    from a no-reranker path carry `rerank_score is None` and fall back to the
    cheap RRF `score`, which is today's behaviour unchanged.
    """
    primary = r.rerank_score if r.rerank_score is not None else r.score
    return (-float(primary), r.document.path)


def _demote_rerank_score(value: float, penalty: float) -> float:
    """Apply `penalty` to a cross-encoder score so it always moves DOWN.

    Cross-encoder outputs are raw logits, and most real ones are NEGATIVE.
    Plain `value * penalty` with `penalty` < 1 shrinks a negative score
    toward zero — which under `_rank_key` is a PROMOTION, and also lifts the
    doc over a negative `auto_recall_min_rerank` threshold. So: multiply
    positives, divide negatives, and leave zero alone (it has no direction to
    move). A non-positive `penalty` means "bury it", which for a negative
    score is `-inf` rather than a division by zero or a sign flip.

    The RRF `score` needs none of this: fused rank reciprocals are always
    positive, so multiplication is already sign-safe there.
    """
    if value > 0.0:
        return value * penalty
    if value < 0.0:
        return value / penalty if penalty > 0.0 else float("-inf")
    return value


def apply_review_policy(
    results: list[QueryResult], policy: str, penalty: float
) -> list[QueryResult]:
    """Down-rank or drop memories flagged `needs_review`.

    - "exclude": flagged memories are removed entirely.
    - "demote":  flagged memories keep their place in the candidate set but
                 BOTH their RRF `score` and their `rerank_score` (when they
                 have one) are penalised, so fresh memories of comparable
                 relevance outrank them. Penalising only the RRF score would
                 let a stale doc with a high cross-encoder score keep the top
                 slot AND sail through `auto_recall_min_rerank` at full
                 strength. The RRF score is scaled (`score * penalty`); the
                 rerank score goes through `_demote_rerank_score`, which is
                 sign-safe because logits are usually negative. Results are
                 re-sorted by `_rank_key` so the caller's top-k truncation
                 reflects the penalty.
    - anything else ("ignore"): returned unchanged.

    Pure and order-stable for non-flagged inputs; safe to call on any list.
    """
    if policy == "ignore" or not results:
        return results
    if policy == "exclude":
        return [r for r in results if not _is_needs_review(r.document)]
    if policy == "demote":
        adjusted = [
            QueryResult(
                document=r.document,
                score=r.score * penalty,
                rerank_score=(
                    _demote_rerank_score(r.rerank_score, penalty)
                    if r.rerank_score is not None
                    else None
                ),
            )
            if _is_needs_review(r.document)
            else r
            for r in results
        ]
        adjusted.sort(key=_rank_key)
        return adjusted
    return results


def _is_superseded(doc: Document) -> bool:
    """True if a document has been replaced by a newer version.

    Goes through `recall.frontmatter.temporal_meta` — the single
    normalization point — so `status: superseded`, a bare `superseded_by`
    pointer, `stance: superseded`, and `type: claim-stale` all count.
    Documents with no temporal fields (everything written before Phase 2)
    are NOT superseded.
    """
    from recall.frontmatter import temporal_meta

    meta = temporal_meta(doc.frontmatter)
    return meta.status == "superseded" or bool(meta.superseded_by)


def _valid_from_sort_value(doc: Document) -> float:
    """valid_from as a sortable epoch; unknown → -inf (sorts last among ties)."""
    import datetime as _dt

    from recall.frontmatter import temporal_meta

    raw = temporal_meta(doc.frontmatter).valid_from
    if not raw:
        return float("-inf")
    try:
        dt = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return float("-inf")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.timestamp()


def _temporal_rank_key(r: QueryResult) -> tuple:
    """_rank_key plus temporal tie-breaks: superseded sinks on equal scores;
    among non-superseded equals, the later valid_from wins."""
    primary = r.rerank_score if r.rerank_score is not None else r.score
    return (
        -float(primary),
        _is_superseded(r.document),
        -_valid_from_sort_value(r.document),
        r.document.path,
    )


def apply_temporal_policy(
    results: list[QueryResult], policy: str, penalty: float
) -> list[QueryResult]:
    """Down-rank or drop memories superseded by a newer version.

    Mirrors apply_review_policy; call it AFTER the review policy so a doc
    that is both unreviewed AND superseded is penalized for both.

    - "exclude": superseded memories are removed entirely.
    - "demote":  RRF `score` is scaled (`score * penalty`) and any
                 `rerank_score` goes through `_demote_rerank_score`
                 (sign-safe for negative logits), then results are
                 re-sorted so the caller's top-k truncation reflects the
                 penalty. Ties prefer non-superseded docs, then the later
                 `valid_from`.
    - anything else ("ignore"): returned unchanged.

    Pure and order-stable for non-superseded inputs; safe to call on any
    list.
    """
    if policy == "ignore" or not results:
        return results
    if policy == "exclude":
        return [r for r in results if not _is_superseded(r.document)]
    if policy == "demote":
        adjusted = [
            QueryResult(
                document=r.document,
                score=r.score * penalty,
                rerank_score=(
                    _demote_rerank_score(r.rerank_score, penalty)
                    if r.rerank_score is not None
                    else None
                ),
            )
            if _is_superseded(r.document)
            else r
            for r in results
        ]
        adjusted.sort(key=_temporal_rank_key)
        return adjusted
    return results


# ---------------------------------------------------------------------------
# HybridRetriever facade
# ---------------------------------------------------------------------------


class HybridRetriever:
    """Hybrid (dense + sparse) retriever backed by Qdrant embedded mode.

    Two construction patterns:

    1. **From in-memory documents** (tests, MCP one-shot, ad-hoc): pass
       `documents=[...]`. Each doc is upserted into a per-source collection
       on construction. Idempotent because point IDs are deterministic
       UUID5s of `Document.path`.

    2. **Cold-start against an already-indexed brain** (CLI hot path):
       pass `documents=None` and `collections=[s.name for s in cfg.sources]`.
       No embedding work happens at construction time — the existing Qdrant
       collections are queried directly.

    Legacy kwargs (`bm25_weight`, `embedding_weight`, `embedding_model`)
    are accepted-and-ignored so old callers / configs keep working without
    edits during the migration window.
    """

    def __init__(
        self,
        documents: Optional[Sequence[Document]] = None,
        *,
        collections: Optional[Sequence[str]] = None,
        embedder: str = "BAAI/bge-base-en-v1.5",
        sparse_embedder: str = "Qdrant/bm25",
        reranker: str = "none",
        reranker_model: str = "jinaai/jina-reranker-v1-turbo-en",
        rerank_n: int = 20,
        needs_review_policy: str = "demote",
        needs_review_penalty: float = 0.5,
        superseded_policy: str = "demote",
        superseded_penalty: float = 0.5,
        mode: str = "hybrid",
        # Legacy kwargs accepted for back-compat; ignored.
        bm25_weight: Optional[float] = None,
        embedding_weight: Optional[float] = None,
        embedding_model: Optional[str] = None,
    ):
        from recall import qdrant_backend as qb
        from recall.config import cache_dir

        self._dense_model = embedder
        self._sparse_model = sparse_embedder
        self._reranker = reranker
        self._reranker_model = reranker_model
        self._rerank_n = int(rerank_n)
        self._needs_review_policy = needs_review_policy
        self._needs_review_penalty = float(needs_review_penalty)
        self._superseded_policy = superseded_policy
        self._superseded_penalty = float(superseded_penalty)
        # Retrieval mode: "hybrid" (dense + sparse), "dense", or "sparse".
        # Passed through to every backend upsert/query so sparse mode never
        # touches the dense embedder (works before the bge download).
        self._mode = mode
        self._client = qb._qdrant_client_singleton(cache_dir())
        self.documents: list[Document] = list(documents) if documents else []

        # Track which collections this facade can query. Union of any
        # explicit list + sources observed in the documents arg.
        self._collections: set[str] = set(collections or [])
        self._collections.update(d.source for d in self.documents)

        # Ensure every target collection exists (idempotent).
        for coll in self._collections:
            qb.ensure_collection(self._client, coll)

        # Upsert any in-memory documents.
        if self.documents:
            by_source: dict[str, list[Document]] = defaultdict(list)
            for d in self.documents:
                by_source[d.source].append(d)
            for source_name, docs in by_source.items():
                qb.upsert_documents(
                    self._client,
                    source_name,
                    docs,
                    dense_model=self._dense_model,
                    sparse_model=self._sparse_model,
                    mode=self._mode,
                )

    def query(
        self,
        query: str,
        k: int,
        type_filter: Optional[str] = None,
        source_filter: Optional[str] = None,
        # S4: explicit override for whether to rerank. `None` (default)
        # preserves today's behavior (driven by `self._reranker`); a caller
        # (the daemon holds ONE retriever and flips reranking per request)
        # may force True/False.
        rerank: Optional[bool] = None,
    ) -> list[QueryResult]:
        from recall import qdrant_backend as qb

        if k <= 0:
            return []
        # Determine which collection(s) to search:
        # - explicit source_filter narrows to that one collection
        # - else union over self._collections
        targets = [source_filter] if source_filter else sorted(self._collections)
        if not targets:
            return []

        use_rerank = self._reranker == "cross_encoder" if rerank is None else bool(rerank)

        # Candidate pool depth. Two things want more than k:
        #   * a needs_review policy (demote/exclude): a fresh memory ranked
        #     just below a flagged one should take the freed slot rather than
        #     leaving a hole or keeping the stale one because it was in the
        #     top-k window;
        #   * the cross-encoder: it can only reorder what the RRF leg pulled,
        #     so with a k-deep pool `rerank_n` means nothing and a candidate
        #     just below the RRF top-k can never be promoted.
        # policy=ignore (both policies) with reranking off is the cheap
        # path; keep it k.
        if (self._needs_review_policy == "ignore"
                and self._superseded_policy == "ignore"):
            fetch_n = max(k, self._rerank_n) if use_rerank else k
        else:
            fetch_n = max(2 * k, self._rerank_n, k + 10)

        # RRF leg: over-fetch from EVERY collection. The deeper pull is what
        # lets a demoted needs_review memory be replaced by a fresh one ranked
        # just below it, rather than leaving a hole. Under
        # superseded_policy="exclude" the backend pre-filters superseded docs
        # so they never consume candidate-pool slots.
        merged: list[QueryResult] = []
        for coll in targets:
            merged.extend(
                qb.query_hybrid(
                    self._client,
                    coll,
                    query,
                    fetch_n,
                    type_filter=type_filter,
                    source_filter=None,  # already constrained by collection
                    dense_model=self._dense_model,
                    sparse_model=self._sparse_model,
                    mode=self._mode,
                    exclude_superseded=self._superseded_policy == "exclude",
                )
            )
        # Order the merged pool by RRF before the cross-encoder sees it, so
        # `rerank_n` selects the globally best candidates rather than an
        # arbitrary per-collection slice.
        merged.sort(key=_rank_key)

        if use_rerank:
            # ONE cross-encoder pass over the merged pool. `rerank_n` is a
            # total budget across all collections: reranking per collection
            # multiplied the cost by the number of sources and made the
            # setting mean nothing at small k. Never rerank fewer than k, or
            # we could not fill the requested page.
            merged = qb.rerank_results(
                query,
                merged,
                reranker_model=self._reranker_model,
                limit=max(self._rerank_n, k),
            )
            # Re-sort: rerank score when present, else RRF score; path breaks
            # ties. Only reranking can change the order — without it `merged`
            # is still in the `_rank_key` order it was sorted into above, and
            # re-sorting it was a full sort to reach the same list.
            merged.sort(key=_rank_key)
        # Down-rank / drop needs_review memories, then superseded ones
        # (review policy first — a doc can be both), then truncate to k.
        merged = apply_review_policy(
            merged, self._needs_review_policy, self._needs_review_penalty
        )
        merged = apply_temporal_policy(
            merged, self._superseded_policy, self._superseded_penalty
        )
        return merged[:k]
