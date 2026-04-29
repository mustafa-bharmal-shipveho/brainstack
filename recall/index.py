"""Index lifecycle for Qdrant-backed recall: build, load, refresh-on-stale.

Documents live in Qdrant collections (one per source). Pre-Qdrant JSON manifests
at $XDG_CACHE_HOME/recall/files.json and $XDG_CACHE_HOME/recall/<source>/files.json
are removed on the first new-format reindex so the cache layout stays clean.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

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


def build_index(sources: Iterable[SourceConfig]) -> IndexCache:
    """Discover docs per source, ensure_collection, upsert into Qdrant."""
    sources_list = list(sources)
    base = cache_dir()
    base.mkdir(parents=True, exist_ok=True)
    _legacy_cache_cleanup(base)

    client = qb._qdrant_client_singleton(base)
    all_docs: list[Document] = []
    for source in sources_list:
        docs = list(discover_documents(source))
        qb.ensure_collection(client, source.name)
        qb.upsert_documents(client, source.name, docs)
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
