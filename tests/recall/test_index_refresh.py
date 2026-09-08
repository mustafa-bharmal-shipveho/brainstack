"""Chunked index refresh: the lock is held per chunk, never for a whole pass.

The daemon owns the embedded Qdrant store, and every client call — query,
upsert, delete, meta scroll — has to go through one lock because embedded
Qdrant is not thread-safe. That makes the refresh pass a direct competitor
with live queries: hold the lock for a whole pass and a burst of imports
blocks every prompt for minutes.

`refresh_index_chunked` splits the pass so that:

  * discovery and mtime comparison (the slow filesystem walk) run OUTSIDE the
    lock entirely;
  * embedding + upsert take the lock in chunks of `chunk_size` docs, so a
    concurrent query waits at most one chunk (~150-300 ms for 4 docs on CPU);
  * the stale-point delete takes it once at the end.

These tests fake the Qdrant backend and count lock acquisitions. They are
about the LOCKING SHAPE, not about retrieval quality, so nothing is embedded
and no model is downloaded.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from recall.config import SourceConfig
from recall.core import Document
from recall.index import RefreshResult, refresh_index_chunked


class _CountingLock:
    """A real lock that records how many times it was taken and whether it is
    held right now. `held` is what lets the qb fakes assert their own call
    happened inside (or outside) the critical section."""

    def __init__(self):
        self._lock = threading.Lock()
        self.acquisitions = 0
        self.held = False

    def __enter__(self):
        self._lock.acquire()
        self.acquisitions += 1
        self.held = True
        return self

    def __exit__(self, *exc):
        self.held = False
        self._lock.release()
        return False

    def acquire(self, *args, **kwargs):
        acquired = self._lock.acquire(*args, **kwargs)
        if acquired:
            self.acquisitions += 1
            self.held = True
        return acquired

    def release(self):
        self.held = False
        self._lock.release()

    def locked(self):
        return self._lock.locked()


class _FakeQB:
    """Stand-in for `recall.qdrant_backend`. Records (call, lock_held)."""

    def __init__(self, lock: _CountingLock, meta: dict | None = None):
        self.lock = lock
        self.meta = dict(meta or {})
        self.calls: list[tuple] = []

    def _qdrant_client_singleton(self, cache_dir):
        self.calls.append(("client", self.lock.held))
        return object()

    def ensure_collection(self, client, collection, *args, **kwargs):
        self.calls.append(("ensure", self.lock.held))

    def _collection_index_meta(self, client, collection):
        self.calls.append(("meta", self.lock.held))
        return dict(self.meta)

    def upsert_documents(self, client, collection, docs, *args, **kwargs):
        docs = list(docs)
        self.calls.append(("upsert", self.lock.held, len(docs)))
        return len(docs)

    def delete_points_not_in_paths(self, client, collection, current_paths, **kwargs):
        """Mirror the real backend: delete indexed paths absent from the
        current source set and return HOW MANY were deleted.

        Set arithmetic, not a length subtraction. `len(meta) - len(current)`
        happens to be right only when the two sets nest; with 1 stale point
        and 2 live docs it returns 0 and the fake would disagree with a
        correct implementation.
        """
        current = set(current_paths)
        stale = [p for p in self.meta if p not in current]
        self.calls.append(("delete", self.lock.held, len(current)))
        for p in stale:
            del self.meta[p]
        return len(stale)

    def upsert_sizes(self) -> list[int]:
        return [c[2] for c in self.calls if c[0] == "upsert"]

    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]


def _make_docs(tmp_path: Path, n: int, *, source: str = "brain") -> list[Document]:
    """Real files on disk: the changed-doc rule reads `os.stat(...).st_mtime`."""
    docs = []
    root = tmp_path / "brain"
    root.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        p = root / f"memory_{i}.md"
        p.write_text(f"---\nname: memory-{i}\n---\nbody {i}\n", encoding="utf-8")
        docs.append(
            Document(
                path=str(p),
                source=source,
                title=f"memory-{i}",
                frontmatter={"name": f"memory-{i}"},
                body=f"body {i}",
                text=f"memory-{i}\nbody {i}",
            )
        )
    return docs


@pytest.fixture
def source(tmp_path: Path) -> SourceConfig:
    root = tmp_path / "brain"
    root.mkdir(parents=True, exist_ok=True)
    return SourceConfig(
        name="brain",
        path=str(root),
        glob="**/*.md",
        frontmatter="optional",
        exclude=[],
    )


def test_chunked_refresh_takes_lock_per_chunk_not_whole_pass(
    tmp_path, monkeypatch, isolated_xdg, source
):
    """9 changed docs at chunk_size 4 → 3 upserts, each under its own acquire.

    Five acquisitions total: one for ensure+meta, three for the chunks, one
    for the stale delete. A single acquisition around the whole pass would
    also produce the right index and would fail here — which is the point.
    """
    lock = _CountingLock()
    docs = _make_docs(tmp_path, 9)
    fake_qb = _FakeQB(lock, meta={})
    monkeypatch.setattr("recall.index.qb", fake_qb)
    monkeypatch.setattr("recall.index.discover_documents", lambda s: iter(docs))

    result = refresh_index_chunked(
        [source], mode="hybrid", lock=lock, chunk_size=4
    )

    assert fake_qb.upsert_sizes() == [4, 4, 1], (
        f"expected three chunked upserts of 4/4/1, got {fake_qb.upsert_sizes()}"
    )
    assert lock.acquisitions == 5, (
        f"expected 5 lock acquisitions (meta + 3 chunks + delete), got "
        f"{lock.acquisitions}; call order was {fake_qb.kinds()}"
    )
    for call in fake_qb.calls:
        if call[0] in {"meta", "upsert", "delete"}:
            assert call[1] is True, f"{call[0]} ran OUTSIDE the lock: {call}"
    assert lock.held is False
    assert result.changed == 9


def test_discovery_runs_outside_lock(tmp_path, monkeypatch, isolated_xdg, source):
    """The filesystem walk must not block queries.

    Discovery over a 700-doc brain is the slowest part of a pass. Holding the
    lock across it is exactly the stall this design exists to avoid.
    """
    lock = _CountingLock()
    docs = _make_docs(tmp_path, 3)
    fake_qb = _FakeQB(lock, meta={})
    monkeypatch.setattr("recall.index.qb", fake_qb)

    held_during_discovery: list[bool] = []

    def _discover(_source):
        held_during_discovery.append(lock.held)
        return iter(docs)

    monkeypatch.setattr("recall.index.discover_documents", _discover)

    refresh_index_chunked([source], mode="hybrid", lock=lock, chunk_size=4)

    assert held_during_discovery == [False], (
        "discover_documents ran while holding the retrieval lock"
    )


def test_on_pending_called_true_then_result_reports_changed_and_deleted(
    tmp_path, monkeypatch, isolated_xdg, source
):
    """`on_pending(True)` fires as soon as the pass knows there is work.

    The daemon copies that into `refresh_pending`, which becomes the
    `index_stale` flag on every query answered during the pass.
    """
    lock = _CountingLock()
    docs = _make_docs(tmp_path, 2)
    # One indexed path is gone from the filesystem → one stale delete.
    stale_meta = {"/brain/deleted_memory.md": (1.0, "hybrid")}
    fake_qb = _FakeQB(lock, meta=stale_meta)
    monkeypatch.setattr("recall.index.qb", fake_qb)
    monkeypatch.setattr("recall.index.discover_documents", lambda s: iter(docs))

    pending_calls: list[bool] = []
    result = refresh_index_chunked(
        [source],
        mode="hybrid",
        lock=lock,
        chunk_size=4,
        on_pending=pending_calls.append,
    )

    assert pending_calls and pending_calls[0] is True, pending_calls
    assert result.changed == 2
    assert result.deleted == 1
    assert result.stale_before is True
    assert isinstance(result.ms, int)
    assert result.per_source.get("brain") == 2


def test_unchanged_index_reports_zero_and_pending_false(
    tmp_path, monkeypatch, isolated_xdg, source
):
    """The common case — nothing changed — must do no embedding work.

    A pass that re-upserts unchanged docs would re-run BGE-base over the whole
    brain every 5 minutes and hold the lock for minutes at a time.
    """
    import os

    lock = _CountingLock()
    docs = _make_docs(tmp_path, 3)
    meta = {d.path: (os.stat(d.path).st_mtime, "hybrid") for d in docs}
    fake_qb = _FakeQB(lock, meta=meta)
    monkeypatch.setattr("recall.index.qb", fake_qb)
    monkeypatch.setattr("recall.index.discover_documents", lambda s: iter(docs))

    pending_calls: list[bool] = []
    result = refresh_index_chunked(
        [source],
        mode="hybrid",
        lock=lock,
        chunk_size=4,
        on_pending=pending_calls.append,
    )

    assert fake_qb.upsert_sizes() == [], "unchanged docs must not be re-embedded"
    assert pending_calls == [False], pending_calls
    assert result.changed == 0
    assert result.deleted == 0
    assert result.stale_before is False
    assert isinstance(result, RefreshResult)


def test_mode_mismatch_counts_as_changed(
    tmp_path, monkeypatch, isolated_xdg, source
):
    """A sparse-indexed brain re-embeds on the first hybrid pass.

    Same rule `upsert_documents` already uses; the chunked pass has to compute
    `changed` identically or a BM25-only brain never gains its dense leg.
    """
    import os

    lock = _CountingLock()
    docs = _make_docs(tmp_path, 2)
    meta = {d.path: (os.stat(d.path).st_mtime, "sparse") for d in docs}
    fake_qb = _FakeQB(lock, meta=meta)
    monkeypatch.setattr("recall.index.qb", fake_qb)
    monkeypatch.setattr("recall.index.discover_documents", lambda s: iter(docs))

    result = refresh_index_chunked([source], mode="hybrid", lock=lock, chunk_size=4)

    assert result.changed == 2, "mode mismatch must count as changed"
    assert fake_qb.upsert_sizes() == [2]
