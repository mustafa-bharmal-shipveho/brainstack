"""Index cache: build, load, refresh-on-stale.

Caches discovered documents (and their text) as a manifest. The retriever
itself recomputes BM25/embeddings on load — keeping the cache simple. For
50-1000 doc brains this is plenty fast (<1s).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from recall.config import SourceConfig, cache_dir
from recall.sources import Document, discover_documents


@dataclass
class IndexCache:
    cache_dir: Path
    documents: list[Document]


def _manifest_path() -> Path:
    """Single combined manifest at the cache root."""
    return cache_dir() / "files.json"


def _manifest_for(source: SourceConfig) -> Path:
    """Per-source detail manifest (kept for test parity / debugging)."""
    safe_name = source.name.replace("/", "_").replace(" ", "_")
    return cache_dir() / safe_name / "files.json"


def _gather_mtimes(source: SourceConfig) -> dict[str, float]:
    """Return {relative-path: mtime} for every file the source would discover.

    Uses discover_documents to honor exclude rules.
    """
    mtimes: dict[str, float] = {}
    for doc in discover_documents(source):
        try:
            st = os.stat(doc.path)
            mtimes[doc.path] = st.st_mtime
        except OSError:
            continue
    return mtimes


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON atomically using a per-process unique tmp filename so two
    concurrent writers can't trample each other's tmp file mid-rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    # threading.get_ident() is unique within a process; combined with pid this
    # is collision-free across processes and threads.
    import threading
    tid = threading.get_ident()
    tmp = path.with_suffix(f".json.{pid}.{tid}.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def build_index(sources: Iterable[SourceConfig]) -> IndexCache:
    """Discover all documents and write manifests. Returns the IndexCache.

    Writes a top-level `files.json` at the cache root (used for staleness
    detection across all sources) plus per-source detail manifests for
    debugging. Atomic per-process writes — two concurrent reindex calls
    won't corrupt the manifest.
    """
    sources = list(sources)
    base = cache_dir()
    base.mkdir(parents=True, exist_ok=True)
    all_docs: list[Document] = []
    combined_manifest: dict = {"sources": []}

    for source in sources:
        per_source_path = _manifest_for(source)

        docs = list(discover_documents(source))
        all_docs.extend(docs)

        files = [{"path": d.path, "mtime": _safe_mtime(d.path)} for d in docs]
        per_source = {"source": source.name, "path": source.path, "files": files}

        _atomic_write_json(per_source_path, per_source)
        combined_manifest["sources"].append(per_source)

    _atomic_write_json(_manifest_path(), combined_manifest)

    return IndexCache(cache_dir=base, documents=all_docs)


def load_index(sources: Iterable[SourceConfig]) -> IndexCache | None:
    """Reload the index from disk if the combined manifest exists; else None.

    Note: also re-discovers documents (the retriever needs Document instances).
    The on-disk manifest is just a tripwire for staleness detection.
    """
    sources = list(sources)
    if not _manifest_path().exists():
        return None
    all_docs: list[Document] = []
    for source in sources:
        all_docs.extend(discover_documents(source))
    return IndexCache(cache_dir=cache_dir(), documents=all_docs)


def needs_refresh(sources: Iterable[SourceConfig]) -> bool:
    """Return True if any manifest is missing, malformed, or stale.

    Treats any unparseable / unexpected-shape manifest as stale rather than
    raising — the cache is purely advisory, never authoritative.
    """
    sources = list(sources)
    combined = _manifest_path()
    if not combined.exists():
        return True
    try:
        stored_combined = json.loads(combined.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return True

    # Defensive type checks: a poisoned cache could contain null, scalars, or
    # wrong shapes. Treat anything unexpected as stale.
    if not isinstance(stored_combined, dict):
        return True
    raw_sources = stored_combined.get("sources")
    if not isinstance(raw_sources, list):
        return True

    stored_by_source: dict[str, dict] = {}
    for entry in raw_sources:
        if not isinstance(entry, dict) or "source" not in entry or "files" not in entry:
            return True
        if not isinstance(entry["files"], list):
            return True
        stored_by_source[entry["source"]] = entry

    if {s.name for s in sources} != set(stored_by_source.keys()):
        return True
    for source in sources:
        files_list = stored_by_source[source.name]["files"]
        try:
            stored_files = {
                f["path"]: f["mtime"]
                for f in files_list
                if isinstance(f, dict) and "path" in f and "mtime" in f
            }
        except (TypeError, KeyError):
            return True
        current_files = _gather_mtimes(source)
        if set(stored_files.keys()) != set(current_files.keys()):
            return True
        for path, mtime in current_files.items():
            if stored_files.get(path) != mtime:
                return True
    return False


def _safe_mtime(path: str) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0
