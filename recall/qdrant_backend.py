"""Qdrant embedded-mode backend: dense + sparse hybrid retrieval via FastEmbed.

Owns the QdrantClient lifecycle, FastEmbed model cache, collection schema,
upsert, and Prefetch + Fusion.RRF query. Module-private — callers go through
recall.core.HybridRetriever or recall.index.build_index.
"""

from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path
from typing import Optional, Sequence

from qdrant_client import QdrantClient, models

from recall.core import Document, QueryResult

# FastEmbed types are imported lazily so unit tests that monkeypatch the
# embedder factories don't pay the import cost.
_DENSE_DEFAULT = "BAAI/bge-base-en-v1.5"
_SPARSE_DEFAULT = "Qdrant/bm25"
_RERANKER_DEFAULT = "jinaai/jina-reranker-v1-turbo-en"
_DENSE_DIM = 768  # bge-base-en-v1.5 output dim
_HYBRID_PREFETCH = 20  # over-pull from each leg before RRF
_RERANK_OVERSAMPLE = 20  # candidates fed to the reranker before truncating to k


# ---------------------------------------------------------------------------
# Client + embedder caches (process-wide singletons, lazy)
# ---------------------------------------------------------------------------

_client_lock = threading.Lock()
_clients: dict[str, QdrantClient] = {}

_embedder_lock = threading.Lock()
_dense_embedders: dict[str, object] = {}
_sparse_embedders: dict[str, object] = {}
_cross_encoders: dict[str, object] = {}


def _qdrant_client_singleton(cache_dir: Path) -> QdrantClient:
    """One QdrantClient per cache directory.

    Embedded Qdrant takes a directory lock — opening twice in one process raises.
    Cache by absolute path so tests with isolated_xdg get fresh clients per tmp dir.
    """
    key = str(Path(cache_dir).resolve())
    with _client_lock:
        client = _clients.get(key)
        if client is None:
            qdrant_path = Path(cache_dir) / "qdrant"
            qdrant_path.mkdir(parents=True, exist_ok=True)
            client = QdrantClient(path=str(qdrant_path))
            _clients[key] = client
        return client


def _reset_client_cache_for_tests() -> None:
    """Test hook: drop client handles so tmp-dir embedded DBs get garbage collected
    and the file lock is released between tests.
    """
    with _client_lock:
        for c in _clients.values():
            try:
                c.close()
            except Exception:
                pass
        _clients.clear()


def _reset_model_cache_for_tests() -> None:
    """Test hook: drop the lazy embedder/cross-encoder caches. Useful when a test
    swaps in a stub model — without this the previous instance survives.
    """
    with _embedder_lock:
        _dense_embedders.clear()
        _sparse_embedders.clear()
        _cross_encoders.clear()


def _get_embedder(name: str = _DENSE_DEFAULT):
    from fastembed import TextEmbedding

    with _embedder_lock:
        emb = _dense_embedders.get(name)
        if emb is None:
            emb = TextEmbedding(model_name=name)
            _dense_embedders[name] = emb
        return emb


def _get_sparse_embedder(name: str = _SPARSE_DEFAULT):
    from fastembed import SparseTextEmbedding

    with _embedder_lock:
        emb = _sparse_embedders.get(name)
        if emb is None:
            emb = SparseTextEmbedding(model_name=name)
            _sparse_embedders[name] = emb
        return emb


def _get_cross_encoder(name: str = _RERANKER_DEFAULT):
    """Lazy-load FastEmbed cross-encoder. Used for the third reranking stage."""
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    with _embedder_lock:
        ce = _cross_encoders.get(name)
        if ce is None:
            ce = TextCrossEncoder(model_name=name)
            _cross_encoders[name] = ce
        return ce


# ---------------------------------------------------------------------------
# Collection lifecycle
# ---------------------------------------------------------------------------


def ensure_collection(
    client: QdrantClient, collection: str, dense_size: int = _DENSE_DIM
) -> None:
    """Idempotent: create the collection with named dense+sparse vectors if missing."""
    if client.collection_exists(collection):
        return
    client.create_collection(
        collection_name=collection,
        vectors_config={
            "dense": models.VectorParams(size=dense_size, distance=models.Distance.COSINE),
        },
        sparse_vectors_config={
            "sparse": models.SparseVectorParams(modifier=models.Modifier.IDF),
        },
    )


def _doc_id(path: str) -> str:
    """Deterministic UUID5 from path so re-upserts merge instead of duplicating."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"recall://{path}"))


def upsert_documents(
    client: QdrantClient,
    collection: str,
    docs: Sequence[Document],
    dense_model: str = _DENSE_DEFAULT,
    sparse_model: str = _SPARSE_DEFAULT,
    batch_size: int = 64,
) -> int:
    """Embed + upsert. Returns number of points written. Idempotent across runs."""
    if not docs:
        return 0
    dense = _get_embedder(dense_model)
    sparse = _get_sparse_embedder(sparse_model)
    texts = [d.text for d in docs]
    dense_vecs = list(dense.embed(texts))
    sparse_vecs = list(sparse.embed(texts))

    points: list[models.PointStruct] = []
    for d, dv, sv in zip(docs, dense_vecs, sparse_vecs):
        try:
            mtime = os.stat(d.path).st_mtime
        except OSError:
            mtime = 0.0
        points.append(
            models.PointStruct(
                id=_doc_id(d.path),
                vector={
                    "dense": list(map(float, dv)),
                    "sparse": models.SparseVector(
                        indices=list(map(int, sv.indices)),
                        values=list(map(float, sv.values)),
                    ),
                },
                payload={
                    "path": d.path,
                    "source": d.source,
                    "title": d.title,
                    "frontmatter": dict(d.frontmatter or {}),
                    "body": d.body,
                    "text": d.text,
                    "mtime": mtime,
                },
            )
        )
    for i in range(0, len(points), batch_size):
        client.upsert(collection_name=collection, points=points[i : i + batch_size])
    return len(points)


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


def _build_filter(
    type_filter: Optional[str], source_filter: Optional[str]
) -> Optional[models.Filter]:
    must: list[models.FieldCondition] = []
    if type_filter is not None:
        must.append(
            models.FieldCondition(
                key="frontmatter.type", match=models.MatchValue(value=type_filter)
            )
        )
    if source_filter is not None:
        must.append(
            models.FieldCondition(key="source", match=models.MatchValue(value=source_filter))
        )
    return models.Filter(must=must) if must else None


def query_hybrid(
    client: QdrantClient,
    collection: str,
    query: str,
    k: int,
    type_filter: Optional[str] = None,
    source_filter: Optional[str] = None,
    dense_model: str = _DENSE_DEFAULT,
    sparse_model: str = _SPARSE_DEFAULT,
) -> list[QueryResult]:
    """Prefetch dense + sparse, fuse with RRF, return top-k as QueryResult."""
    if k <= 0:
        return []
    if not client.collection_exists(collection):
        return []

    dense_vec = list(map(float, next(iter(_get_embedder(dense_model).query_embed([query])))))
    sv = next(iter(_get_sparse_embedder(sparse_model).query_embed([query])))
    sparse_vec = models.SparseVector(
        indices=list(map(int, sv.indices)), values=list(map(float, sv.values))
    )

    flt = _build_filter(type_filter, source_filter)
    prefetch = [
        models.Prefetch(query=dense_vec, using="dense", limit=_HYBRID_PREFETCH, filter=flt),
        models.Prefetch(query=sparse_vec, using="sparse", limit=_HYBRID_PREFETCH, filter=flt),
    ]
    resp = client.query_points(
        collection_name=collection,
        prefetch=prefetch,
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=k,
        with_payload=True,
        query_filter=flt,
    )

    out: list[QueryResult] = []
    for sp in resp.points:
        payload = sp.payload or {}
        doc = Document(
            path=payload.get("path", ""),
            source=payload.get("source", ""),
            title=payload.get("title", ""),
            frontmatter=payload.get("frontmatter") or {},
            body=payload.get("body", ""),
            text=payload.get("text", ""),
        )
        out.append(QueryResult(document=doc, score=float(sp.score)))
    return out


def query_hybrid_rerank(
    client: QdrantClient,
    collection: str,
    query: str,
    k: int,
    type_filter: Optional[str] = None,
    source_filter: Optional[str] = None,
    dense_model: str = _DENSE_DEFAULT,
    sparse_model: str = _SPARSE_DEFAULT,
    reranker_model: str = _RERANKER_DEFAULT,
    rerank_n: int = _RERANK_OVERSAMPLE,
) -> list[QueryResult]:
    """Hybrid query + cross-encoder rerank.

    1. Pull top-`rerank_n` from `query_hybrid` (oversample)
    2. Score every (query, doc.text) pair with the cross-encoder
    3. Sort by rerank score descending, return top-k

    The cross-encoder scores are 0-1 floats from FastEmbed and replace the
    Qdrant fusion score in the returned `QueryResult.score` for transparency.
    """
    if k <= 0:
        return []
    n = max(rerank_n, k)
    candidates = query_hybrid(
        client,
        collection,
        query,
        n,
        type_filter=type_filter,
        source_filter=source_filter,
        dense_model=dense_model,
        sparse_model=sparse_model,
    )
    if not candidates:
        return []
    if len(candidates) <= k:
        # No rerank value when we already have <=k candidates; return as is
        return candidates

    encoder = _get_cross_encoder(reranker_model)
    texts = [c.document.text for c in candidates]
    rerank_scores = list(encoder.rerank(query, texts))
    paired = list(zip(candidates, rerank_scores))
    paired.sort(key=lambda x: -float(x[1]))
    return [
        QueryResult(document=c.document, score=float(s))
        for c, s in paired[:k]
    ]


def count(client: QdrantClient, collection: str) -> int:
    if not client.collection_exists(collection):
        return 0
    return int(client.count(collection_name=collection, exact=True).count)


def collection_mtimes(client: QdrantClient, collection: str) -> dict[str, float]:
    """Return {path: mtime} for every point. Used by needs_refresh."""
    if not client.collection_exists(collection):
        return {}
    out: dict[str, float] = {}
    next_offset = None
    while True:
        points, next_offset = client.scroll(
            collection_name=collection,
            limit=1024,
            with_payload=["path", "mtime"],
            with_vectors=False,
            offset=next_offset,
        )
        for p in points:
            payload = p.payload or {}
            path = payload.get("path")
            if isinstance(path, str):
                out[path] = float(payload.get("mtime") or 0.0)
        if next_offset is None:
            break
    return out
