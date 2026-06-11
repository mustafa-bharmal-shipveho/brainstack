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


def apply_review_policy(
    results: list[QueryResult], policy: str, penalty: float
) -> list[QueryResult]:
    """Down-rank or drop memories flagged `needs_review`.

    - "exclude": flagged memories are removed entirely.
    - "demote":  flagged memories keep their place in the candidate set but
                 their score is multiplied by `penalty`, so fresh memories of
                 comparable relevance outrank them. Results are re-sorted
                 (score desc, then path) so the caller's top-k truncation
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
            QueryResult(document=r.document, score=r.score * penalty)
            if _is_needs_review(r.document)
            else r
            for r in results
        ]
        adjusted.sort(key=lambda r: (-r.score, r.document.path))
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

        # When a needs_review policy is active we fetch a deeper candidate
        # pool than k: demoting or excluding a flagged memory should let a
        # fresh memory ranked just below it take the freed slot, rather than
        # leaving a hole or keeping the stale one only because it was in the
        # top-k window.
        if self._needs_review_policy == "ignore":
            fetch_n = k
        else:
            fetch_n = max(2 * k, self._rerank_n, k + 10)

        merged: list[QueryResult] = []
        use_rerank = self._reranker == "cross_encoder"
        for coll in targets:
            if use_rerank:
                # Return fetch_n reranked results (headroom for the
                # needs_review policy, which may demote/drop some before the
                # final top-k truncation). query_hybrid_rerank reranks a pool
                # of max(rerank_n, fetch_n) and only skips when there's nothing
                # to reorder, so the cross-encoder runs even when the corpus is
                # smaller than fetch_n.
                merged.extend(
                    qb.query_hybrid_rerank(
                        self._client,
                        coll,
                        query,
                        fetch_n,
                        type_filter=type_filter,
                        source_filter=None,  # already constrained by collection
                        dense_model=self._dense_model,
                        sparse_model=self._sparse_model,
                        reranker_model=self._reranker_model,
                        rerank_n=self._rerank_n,
                        mode=self._mode,
                    )
                )
            else:
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
                    )
                )
        # Stable sort by score desc, then path for determinism
        merged.sort(key=lambda r: (-r.score, r.document.path))
        # Down-rank / drop needs_review memories, then truncate to k.
        merged = apply_review_policy(
            merged, self._needs_review_policy, self._needs_review_penalty
        )
        return merged[:k]
