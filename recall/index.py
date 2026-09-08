"""Index lifecycle for Qdrant-backed recall: build, load, refresh-on-stale.

Documents live in Qdrant collections (one per source). A rebuild upserts the
current source files, then prunes stale points after the upsert succeeds so an
embedding failure does not destroy the previous usable index. Pre-Qdrant JSON
manifests at $XDG_CACHE_HOME/recall/files.json and
$XDG_CACHE_HOME/recall/<source>/files.json are removed on the first new-format
reindex so the cache layout stays clean.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from recall import qdrant_backend as qb
from recall.config import SourceConfig, cache_dir
from recall.core import Document
from recall.sources import discover_documents


@dataclass
class IndexCache:
    cache_dir: Path
    documents: list[Document] = field(default_factory=list)


def _legacy_cache_cleanup(base: Path) -> None:
    """Remove pre-Qdrant JSON manifests if they're still around. No-op if absent."""
    legacy = base / "files.json"
    if legacy.exists():
        try:
            legacy.unlink()
        except OSError:
            pass
    if not base.exists():
        return
    for child in base.iterdir():
        if child.is_dir() and child.name != "qdrant" and (child / "files.json").exists():
            try:
                (child / "files.json").unlink()
            except OSError:
                pass


def build_index(sources: Iterable[SourceConfig], mode: str = "hybrid") -> IndexCache:
    """Discover docs per source, upsert current docs, then prune stale points.

    `mode` is threaded into the upsert so a `sparse` build skips the dense
    embedding leg (storing a zero-vector placeholder) and never constructs or
    downloads the dense model. Defaults to hybrid to preserve prior behavior
    for callers that do not care.
    """
    sources_list = list(sources)
    base = cache_dir()
    base.mkdir(parents=True, exist_ok=True)
    _legacy_cache_cleanup(base)

    client = qb._qdrant_client_singleton(base)
    all_docs: list[Document] = []
    for source in sources_list:
        docs = list(discover_documents(source))
        qb.ensure_collection(client, source.name)
        qb.upsert_documents(client, source.name, docs, mode=mode)
        qb.delete_points_not_in_paths(client, source.name, {d.path for d in docs})
        all_docs.extend(docs)
    return IndexCache(cache_dir=base, documents=all_docs)


def load_index(sources: Iterable[SourceConfig]) -> Optional[IndexCache]:
    """Re-discover docs from the filesystem (sources is the truth; Qdrant is the index).

    Returns None when no source's collection has any points yet — keeps cli.py's
    cold-start fall-through (prints "[]") working.
    """
    sources_list = list(sources)
    client = qb._qdrant_client_singleton(cache_dir())
    if not any(qb.count(client, s.name) > 0 for s in sources_list):
        return None
    all_docs: list[Document] = []
    for source in sources_list:
        all_docs.extend(discover_documents(source))
    return IndexCache(cache_dir=cache_dir(), documents=all_docs)


@dataclass
class RefreshResult:
    """Outcome of one `refresh_index_chunked` pass (S3 daemon freshness)."""

    changed: int
    deleted: int
    stale_before: bool
    ms: int
    per_source: dict[str, int] = field(default_factory=dict)


def refresh_index_chunked(
    sources: Iterable[SourceConfig],
    *,
    mode: str,
    lock: "threading.Lock",
    chunk_size: int = 4,
    on_pending: "Callable[[bool], None] | None" = None,
) -> RefreshResult:
    """Daemon-owned index refresh: discovery outside the lock, upserts in
    small chunks under it (S3).

    Discovery and mtime comparison (the slow filesystem walk) run OUTSIDE
    `lock` entirely. Embedding + upsert take `lock` in chunks of
    `chunk_size` docs, so a concurrent query waits at most one chunk. The
    stale-point delete takes `lock` once at the end. `on_pending(True)`
    fires as soon as the pass knows there is work, before the first chunk
    is upserted, so the daemon can report `index_stale` mid-pass.

    The changed-doc rule is deliberately identical to
    `qdrant_backend.upsert_documents`: a doc is changed when it has no
    indexed point, when its source mtime differs, or when the recorded
    index mode differs from `mode` (so a sparse-indexed brain gains its
    dense leg on the first hybrid pass). Computing it differently here
    would either re-embed the whole brain every pass or never re-embed at
    all.

    Failures propagate. The daemon turns them into `last_refresh_ok=False`
    plus `index_stale`, which is the whole point — a silently-swallowed
    pass would serve a stale brain forever with nothing to show for it.
    """
    started = time.perf_counter()
    sources_list = list(sources)
    base = cache_dir()
    base.mkdir(parents=True, exist_ok=True)

    client = None
    total_changed = 0
    total_deleted = 0
    per_source: dict[str, int] = {}

    for source in sources_list:
        # OUTSIDE the lock: the filesystem walk over a 700-doc brain is the
        # slowest part of a pass and must never block a live query.
        docs = list(discover_documents(source))
        current_paths = {d.path for d in docs}

        with lock:
            if client is None:
                client = qb._qdrant_client_singleton(base)
            qb.ensure_collection(client, source.name)
            meta = qb._collection_index_meta(client, source.name)

        # Also outside the lock: stat() per doc plus the set arithmetic.
        changed: list[Document] = []
        for doc in docs:
            try:
                current_mtime = os.stat(doc.path).st_mtime
            except OSError:
                current_mtime = 0.0
            prev = meta.get(doc.path)
            if prev is None or prev[0] != current_mtime or prev[1] != mode:
                changed.append(doc)
        stale_paths = set(meta) - current_paths

        if on_pending is not None:
            on_pending(bool(changed or stale_paths))

        per_source[source.name] = len(changed)
        total_changed += len(changed)

        # Chunked upserts: one lock acquisition per chunk, so a concurrent
        # query waits at most one chunk's embed (~150-300 ms for 4 docs on
        # CPU) instead of the whole pass.
        step = max(1, int(chunk_size))
        for start in range(0, len(changed), step):
            chunk = changed[start:start + step]
            with lock:
                qb.upsert_documents(client, source.name, chunk, mode=mode)

        # Prune AFTER the upserts, exactly as `build_index` does: an
        # embedding failure mid-pass must not also destroy the points that
        # are still usable.
        with lock:
            total_deleted += qb.delete_points_not_in_paths(
                client, source.name, current_paths
            )

    return RefreshResult(
        changed=total_changed,
        deleted=total_deleted,
        stale_before=bool(total_changed or total_deleted),
        ms=int((time.perf_counter() - started) * 1000),
        per_source=per_source,
    )


def needs_refresh(sources: Iterable[SourceConfig]) -> bool:
    """True if any source's filesystem state differs from its Qdrant collection."""
    sources_list = list(sources)
    client = qb._qdrant_client_singleton(cache_dir())
    for source in sources_list:
        if not client.collection_exists(source.name):
            return True
        stored = qb.collection_mtimes(client, source.name)
        current: dict[str, float] = {}
        for doc in discover_documents(source):
            try:
                current[doc.path] = os.stat(doc.path).st_mtime
            except OSError:
                continue
        if set(stored.keys()) != set(current.keys()):
            return True
        for path, mt in current.items():
            if stored.get(path) != mt:
                return True
    return False
