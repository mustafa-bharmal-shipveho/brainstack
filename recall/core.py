"""Recall public surface: Document, QueryResult, HybridRetriever facade.

HybridRetriever is the only retrieval entrypoint. It delegates dense+sparse
hybrid scoring to recall.qdrant_backend; this module holds only the public
dataclasses and a thin facade so cli.py / mcp_server.py / tests don't have
to know about Qdrant.
"""

from __future__ import annotations

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

        merged: list[QueryResult] = []
        use_rerank = self._reranker == "cross_encoder"
        for coll in targets:
            if use_rerank:
                merged.extend(
                    qb.query_hybrid_rerank(
                        self._client,
                        coll,
                        query,
                        k,
                        type_filter=type_filter,
                        source_filter=None,  # already constrained by collection
                        dense_model=self._dense_model,
                        sparse_model=self._sparse_model,
                        reranker_model=self._reranker_model,
                        rerank_n=self._rerank_n,
                    )
                )
            else:
                merged.extend(
                    qb.query_hybrid(
                        self._client,
                        coll,
                        query,
                        k,
                        type_filter=type_filter,
                        source_filter=None,  # already constrained by collection
                        dense_model=self._dense_model,
                        sparse_model=self._sparse_model,
                    )
                )
        # Stable sort by score desc, then path for determinism
        merged.sort(key=lambda r: (-r.score, r.document.path))
        return merged[:k]
