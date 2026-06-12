"""Qdrant embedded-mode backend: dense + sparse hybrid retrieval via FastEmbed.

Owns the QdrantClient lifecycle, FastEmbed model cache, collection schema,
upsert, and Prefetch + Fusion.RRF query. Module-private — callers go through
recall.core.HybridRetriever or recall.index.build_index.
"""

from __future__ import annotations

import atexit
import errno
import os
import sys
import threading
import time
import uuid
import warnings
from pathlib import Path
from typing import NoReturn, Optional, Sequence, TextIO

try:  # pragma: no cover - fcntl is unavailable on Windows.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

# Embedded Qdrant + Windows is unsafe in concurrent use: without fcntl the
# process-level lock is a no-op, and two recall processes can corrupt the
# embedded store. Warn once on import so silent degradation is audible.
_FCNTL_WARN_ONCE = threading.Event()


def _warn_no_process_lock_once() -> None:
    if _FCNTL_WARN_ONCE.is_set() or fcntl is not None:
        return
    _FCNTL_WARN_ONCE.set()
    warnings.warn(
        "recall: fcntl unavailable on this platform (sys.platform="
        f"{sys.platform!r}); embedded-Qdrant process-level locking is "
        "DISABLED. Concurrent recall processes can corrupt the cache. "
        "Set XDG_CACHE_HOME to a unique directory per process, or run "
        "Qdrant server-mode.",
        RuntimeWarning,
        stacklevel=2,
    )

from qdrant_client import QdrantClient, models

from recall.core import Document, QueryResult

# Dense-embedder failures in hybrid mode degrade to BM25-only retrieval.
# Warn once per process (mirrors _FCNTL_WARN_ONCE) so a brain whose bge
# download never completed stays queryable instead of failing every call.
_SPARSE_FALLBACK_WARN_ONCE = threading.Event()


def _warn_sparse_fallback_once(exc: Exception) -> None:
    if _SPARSE_FALLBACK_WARN_ONCE.is_set():
        return
    _SPARSE_FALLBACK_WARN_ONCE.set()
    print(
        f"recall: dense embedding model unavailable ({exc}); falling back "
        "to BM25-only (sparse) retrieval for this process. Run "
        "'recall reindex' once the model can be downloaded to restore "
        "hybrid quality, or set RECALL_MODE=sparse to make sparse-only "
        "explicit.",
        file=sys.stderr,
    )


def _reset_sparse_fallback_warning_for_tests() -> None:
    """Test hook: re-arm the once-per-process sparse-fallback warning."""
    _SPARSE_FALLBACK_WARN_ONCE.clear()


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
_client_lock_files: dict[str, TextIO] = {}

# Per-cache-dir lock: serializes opens for a single cache_dir within this
# process WITHOUT holding the global `_client_lock` across slow filesystem
# work (the fcntl wait can take up to RECALL_QDRANT_LOCK_TIMEOUT seconds).
# Two threads opening DIFFERENT cache_dirs no longer block each other.
_per_key_lock_guard = threading.Lock()
_per_key_locks: dict[str, threading.Lock] = {}


def _lock_for_key(key: str) -> threading.Lock:
    with _per_key_lock_guard:
        lk = _per_key_locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _per_key_locks[key] = lk
        return lk

_embedder_lock = threading.Lock()
_dense_embedders: dict[str, object] = {}
_sparse_embedders: dict[str, object] = {}
_cross_encoders: dict[str, object] = {}


class QdrantStoreBusyError(RuntimeError):
    """Embedded-Qdrant store is in use by another recall process."""


class QdrantStoreAccessError(RuntimeError):
    """Embedded-Qdrant store cannot be opened due to filesystem access."""


def _qdrant_lock_timeout_seconds() -> float:
    raw = os.environ.get("RECALL_QDRANT_LOCK_TIMEOUT", "2")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 2.0


def _qdrant_busy_message(cache_dir: Path) -> str:
    qdrant_path = Path(cache_dir) / "qdrant"
    return (
        f"embedded Qdrant index is busy at {qdrant_path}; another recall process "
        "is using it. Retry shortly, use a separate XDG_CACHE_HOME, or run a "
        "shared recall/Qdrant service for heavy concurrent agents."
    )


def _qdrant_access_message(cache_dir: Path, exc: OSError) -> str:
    qdrant_path = Path(cache_dir) / "qdrant"
    return (
        f"embedded Qdrant index is not writable at {qdrant_path}; check cache "
        "ownership/permissions or set XDG_CACHE_HOME to a writable directory. "
        f"Cause: {exc}"
    )


def _acquire_qdrant_process_lock(cache_dir: Path) -> TextIO | None:
    if fcntl is None:
        _warn_no_process_lock_once()
        return None

    lock_path = Path(cache_dir) / "qdrant.client.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = lock_path.open("a+", encoding="utf-8")
    except OSError:
        return None

    timeout = _qdrant_lock_timeout_seconds()
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock_file
        except BlockingIOError:
            pass
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                lock_file.close()
                return None

        if time.monotonic() >= deadline:
            lock_file.close()
            raise QdrantStoreBusyError(_qdrant_busy_message(cache_dir))
        time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))


def _release_lock_file(lock_file: Optional[TextIO]) -> None:
    if lock_file is None:
        return
    try:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        lock_file.close()
    except OSError:
        pass


def _qdrant_client_singleton(cache_dir: Path) -> QdrantClient:
    """One QdrantClient per cache directory.

    Embedded Qdrant takes a directory lock — opening twice in one process raises.
    Cache by absolute path so tests with isolated_xdg get fresh clients per tmp dir.

    Locking strategy:
      - _client_lock guards only the _clients dict, never held across slow I/O.
      - _lock_for_key(key) serializes opens for THIS cache_dir; threads opening
        a different cache_dir don't block here.
      - fcntl process-lock + QdrantClient construction happen under the
        per-key lock only, so an unrelated query for a different cache is not
        blocked behind a 2-second fcntl wait.
    """
    key = str(Path(cache_dir).resolve())

    # Fast path: client already exists.
    with _client_lock:
        client = _clients.get(key)
    if client is not None:
        return client

    # Slow path under the per-key lock. Double-check after acquire (another
    # thread for the SAME key may have constructed the client while we
    # waited).
    with _lock_for_key(key):
        with _client_lock:
            client = _clients.get(key)
        if client is not None:
            return client

        qdrant_path = Path(cache_dir) / "qdrant"
        try:
            qdrant_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise QdrantStoreAccessError(_qdrant_access_message(cache_dir, exc)) from exc

        lock_file = _acquire_qdrant_process_lock(cache_dir)
        try:
            new_client = QdrantClient(path=str(qdrant_path))
        except OSError as exc:
            _release_lock_file(lock_file)
            raise QdrantStoreAccessError(_qdrant_access_message(cache_dir, exc)) from exc
        except Exception as exc:
            _release_lock_file(lock_file)
            # qdrant-client raises a RuntimeError with this message when the
            # embedded store is already opened by another QdrantClient (any
            # process or thread). Confirmed against qdrant-client>=1.13
            # (pyproject pin). If the upstream wording shifts in a minor
            # release, this fallback returns the original error unchanged
            # rather than mis-classifying it.
            if (
                isinstance(exc, RuntimeError)
                and "already accessed by another instance of Qdrant client"
                in str(exc)
            ):
                raise QdrantStoreBusyError(_qdrant_busy_message(cache_dir)) from exc
            raise

        # Register. If two threads on different per-key locks somehow raced
        # (shouldn't happen with the per-key lock above, but be defensive)
        # the loser closes and returns the winner's client.
        with _client_lock:
            existing = _clients.get(key)
            if existing is not None:
                _release_lock_file(lock_file)
                try:
                    new_client.close()
                except Exception:
                    pass
                return existing
            _clients[key] = new_client
            if lock_file is not None:
                _client_lock_files[key] = lock_file
        return new_client


def _reset_client_cache_for_tests() -> None:
    """Test hook: drop client handles so tmp-dir embedded DBs get garbage collected
    and the file lock is released between tests.

    Note: the FastEmbed model caches (_dense_embedders, _sparse_embedders,
    _cross_encoders) are intentionally NOT cleared here. Model load is the
    expensive part (hundreds of MB + GPU init); we keep them alive across
    client teardowns so the next query doesn't pay reload cost. Use
    `_reset_model_cache_for_tests` to drop those too.
    """
    with _client_lock:
        for c in _clients.values():
            try:
                c.close()
            except Exception:
                pass
        _clients.clear()
        for lock_file in _client_lock_files.values():
            _release_lock_file(lock_file)
        _client_lock_files.clear()


def close_client_cache() -> None:
    """Close embedded-Qdrant clients and release process-level store locks.

    Suitable for CLI exit (registered via atexit). NOT suitable to call
    after every MCP request — the MCP server is long-lived and the
    singleton exists precisely to amortize embedded-Qdrant open cost
    across many queries. See recall/mcp_server.py.
    """
    _reset_client_cache_for_tests()


atexit.register(close_client_cache)


def _reset_model_cache_for_tests() -> None:
    """Test hook: drop the lazy embedder/cross-encoder caches. Useful when a test
    swaps in a stub model — without this the previous instance survives.
    """
    with _embedder_lock:
        _dense_embedders.clear()
        _sparse_embedders.clear()
        _cross_encoders.clear()


def _fastembed_cache_dir() -> str:
    """Resolve (and create) the directory FastEmbed model weights live in.

    Without an explicit cache_dir, fastembed defaults to
    $TMPDIR/fastembed_cache, which macOS purges periodically. That turns a
    "one-time 440 MB download" into a recurring multi-minute stall. Pin the
    weights somewhere durable instead:

      1. FASTEMBED_CACHE_PATH env var, when set (fastembed's own override)
      2. $XDG_CACHE_HOME/fastembed (default ~/.cache/fastembed)

    Importable on purpose: `recall doctor` reports this exact path, and
    tests assert the factories pass it through.
    """
    override = os.environ.get("FASTEMBED_CACHE_PATH")
    if override:
        path = Path(override).expanduser()
    else:
        xdg = os.environ.get("XDG_CACHE_HOME")
        base = Path(xdg).expanduser() if xdg else Path(os.path.expanduser("~/.cache"))
        path = base / "fastembed"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _get_embedder(name: str = _DENSE_DEFAULT):
    from fastembed import TextEmbedding

    with _embedder_lock:
        emb = _dense_embedders.get(name)
        if emb is None:
            emb = TextEmbedding(model_name=name, cache_dir=_fastembed_cache_dir())
            _dense_embedders[name] = emb
        return emb


def _get_sparse_embedder(name: str = _SPARSE_DEFAULT):
    from fastembed import SparseTextEmbedding

    with _embedder_lock:
        emb = _sparse_embedders.get(name)
        if emb is None:
            emb = SparseTextEmbedding(model_name=name, cache_dir=_fastembed_cache_dir())
            _sparse_embedders[name] = emb
        return emb


def _get_cross_encoder(name: str = _RERANKER_DEFAULT):
    """Lazy-load FastEmbed cross-encoder. Used for the third reranking stage."""
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    with _embedder_lock:
        ce = _cross_encoders.get(name)
        if ce is None:
            ce = TextCrossEncoder(model_name=name, cache_dir=_fastembed_cache_dir())
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
    mode: str = "hybrid",
) -> int:
    """Embed + upsert ONLY docs whose source-file mtime differs from the
    already-indexed value. Returns the number of points written.

    Why: in the common case (brain syncs hourly, dream cycle writes a
    handful of files) `build_index` is called with the full doc set but
    99% of those docs are unchanged. Embedding all of them re-runs
    BGE-base inference on 700+ docs per `recall query` invocation —
    4-5 minutes on CPU. Skipping unchanged docs collapses that to
    sub-second (1-3 changed files embedded).

    Mtime semantics:
      * mtime read from `os.stat(d.path).st_mtime` (the source file)
      * compared against `payload.mtime` of the existing Qdrant point
      * exact float equality (no tolerance) — filesystem mtime precision
        is consistent across syncs; tolerance would mask real updates
      * if the source file is missing (`OSError`), we use the 0.0
        sentinel and re-embed defensively (matches pre-fix behavior)

    Mode semantics:
      * "hybrid" embeds + stores both legs (default; prior behavior)
      * "sparse" never runs the dense embedder (no model download needed);
        a zero-vector placeholder fills the dense slot
      * "dense" skips the sparse leg
      * the effective mode is recorded in each point's payload; a point
        whose recorded mode differs from the requested one is treated as
        changed even when the mtime matches, so a sparse-indexed brain
        re-embeds (gains its dense leg) on the first hybrid run
      * in hybrid mode a dense-embedder failure warns once and degrades
        to the sparse leg instead of failing the whole upsert

    Idempotency across runs: yes (mtime comparison is the new
    short-circuit; without it the function was already idempotent
    via uuid5(path) point ids).
    """
    if not docs:
        return 0

    # Read what's already indexed so we can skip unchanged docs.
    existing_meta = _collection_index_meta(client, collection)

    # Partition: changed (needs embed+upsert) vs unchanged (skip). A mode
    # mismatch counts as changed; an unchanged same-mode point keeps the
    # mtime short-circuit.
    changed: list[tuple[Document, float]] = []
    for d in docs:
        try:
            current_mtime = os.stat(d.path).st_mtime
        except OSError:
            current_mtime = 0.0
        prev = existing_meta.get(d.path)
        if prev is None or prev[0] != current_mtime or prev[1] != mode:
            changed.append((d, current_mtime))

    if not changed:
        return 0

    texts = [d.text for d, _ in changed]
    effective_mode = mode
    dense_vecs: Optional[list] = None
    if mode in ("hybrid", "dense"):
        try:
            dense = _get_embedder(dense_model)
            dense_vecs = list(dense.embed(texts))
        except Exception as exc:
            if mode == "dense":
                raise
            _warn_sparse_fallback_once(exc)
            effective_mode = "sparse"
            dense_vecs = None
    sparse_vecs: Optional[list] = None
    if effective_mode in ("hybrid", "sparse"):
        sparse = _get_sparse_embedder(sparse_model)
        sparse_vecs = list(sparse.embed(texts))

    points: list[models.PointStruct] = []
    for idx, (d, mtime) in enumerate(changed):
        vector: dict = {}
        if dense_vecs is not None:
            vector["dense"] = list(map(float, dense_vecs[idx]))
        else:
            # Sparse mode stores a zero-vector dense placeholder (no
            # embedder needed; scores ~0 against any query). Embedded
            # Qdrant's local _add_point never grows the named dense array
            # for points added without that leg, so a later in-place
            # sparse-to-hybrid upgrade would IndexError; the placeholder
            # keeps the array consistent and the upgrade is a plain
            # re-embed triggered by the recorded-mode mismatch above.
            vector["dense"] = [0.0] * _DENSE_DIM
        if sparse_vecs is not None:
            sv = sparse_vecs[idx]
            vector["sparse"] = models.SparseVector(
                indices=list(map(int, sv.indices)),
                values=list(map(float, sv.values)),
            )
        points.append(
            models.PointStruct(
                id=_doc_id(d.path),
                vector=vector,
                payload={
                    "path": d.path,
                    "source": d.source,
                    "title": d.title,
                    "frontmatter": dict(d.frontmatter or {}),
                    "body": d.body,
                    "text": d.text,
                    "mtime": mtime,
                    "mode": effective_mode,
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
    mode: str = "hybrid",
) -> list[QueryResult]:
    """Prefetch dense + sparse, fuse with RRF, return top-k as QueryResult.

    `mode` selects the legs: "hybrid" (both, RRF-fused), "dense" or
    "sparse" (single leg, direct query, no prefetch/fusion). Sparse mode
    never constructs the dense embedder, so it works before the bge model
    is downloaded. In hybrid mode a dense-embedder failure warns once per
    process and degrades to the sparse leg instead of raising.
    """
    if k <= 0:
        return []
    if not client.collection_exists(collection):
        return []
    # An existing-but-empty collection (the fresh-install `imports` tier before
    # any source is added) cannot be queried: Qdrant's sparse IDF rescoring
    # raises KeyError 'sparse' over an empty corpus. Skip it so a new user's
    # very first query does not crash.
    if count(client, collection) == 0:
        return []

    flt = _build_filter(type_filter, source_filter)

    dense_vec: Optional[list[float]] = None
    if mode in ("hybrid", "dense"):
        try:
            dense_vec = list(
                map(float, next(iter(_get_embedder(dense_model).query_embed([query]))))
            )
        except Exception as exc:
            if mode == "dense":
                raise
            _warn_sparse_fallback_once(exc)
            dense_vec = None

    sparse_vec: Optional[models.SparseVector] = None
    if mode in ("hybrid", "sparse") or dense_vec is None:
        sv = next(iter(_get_sparse_embedder(sparse_model).query_embed([query])))
        sparse_vec = models.SparseVector(
            indices=list(map(int, sv.indices)), values=list(map(float, sv.values))
        )

    if dense_vec is not None and sparse_vec is not None:
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
    else:
        # Single-leg query: no prefetch, no fusion. Used by explicit
        # dense/sparse modes and by the hybrid dense-failure fallback.
        resp = client.query_points(
            collection_name=collection,
            query=dense_vec if dense_vec is not None else sparse_vec,
            using="dense" if dense_vec is not None else "sparse",
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
    mode: str = "hybrid",
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
        mode=mode,
    )
    if not candidates:
        return []
    if len(candidates) <= 1:
        # Nothing to reorder. (We deliberately do NOT short-circuit at <=k:
        # callers oversample `k` for a downstream policy/truncation step, so
        # reranking still determines which candidates survive — skipping it
        # there silently bypassed the cross-encoder.)
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


def _collection_index_meta(
    client: QdrantClient, collection: str
) -> dict[str, tuple[float, str]]:
    """Return {path: (mtime, mode)} for every point. Used by upsert_documents.

    Points written before mode tracking lack the payload key; they were
    indexed with both legs, so they read back as "hybrid" (which preserves
    the mtime short-circuit for unchanged pre-upgrade points).
    """
    if not client.collection_exists(collection):
        return {}
    out: dict[str, tuple[float, str]] = {}
    next_offset = None
    while True:
        points, next_offset = client.scroll(
            collection_name=collection,
            limit=1024,
            with_payload=["path", "mtime", "mode"],
            with_vectors=False,
            offset=next_offset,
        )
        for p in points:
            payload = p.payload or {}
            path = payload.get("path")
            if isinstance(path, str):
                out[path] = (
                    float(payload.get("mtime") or 0.0),
                    str(payload.get("mode") or "hybrid"),
                )
        if next_offset is None:
            break
    return out


def delete_points_not_in_paths(
    client: QdrantClient,
    collection: str,
    current_paths: set[str],
    batch_size: int = 1024,
) -> int:
    """Delete indexed points whose payload path is no longer in the source set.

    Reindex uses this after a successful current-doc upsert. That order keeps
    the previous usable index intact if embedding or upsert fails midway.
    """
    if not client.collection_exists(collection):
        return 0
    stale_ids: list[int | str] = []
    next_offset = None
    while True:
        points, next_offset = client.scroll(
            collection_name=collection,
            limit=1024,
            with_payload=["path"],
            with_vectors=False,
            offset=next_offset,
        )
        for p in points:
            payload = p.payload or {}
            path = payload.get("path")
            if isinstance(path, str) and path not in current_paths:
                stale_ids.append(p.id)
        if next_offset is None:
            break

    for i in range(0, len(stale_ids), batch_size):
        client.delete(
            collection_name=collection,
            points_selector=models.PointIdsList(points=stale_ids[i : i + batch_size]),
        )
    return len(stale_ids)
