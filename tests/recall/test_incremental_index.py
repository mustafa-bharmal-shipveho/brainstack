"""Skip-if-fresh tests for `upsert_documents`.

Today every `recall query` against a brain with modified files re-embeds
all 700+ docs (~4 minutes) even though only 1-2 files actually changed.
`upsert_documents` should consult the existing collection's per-point
mtime and skip embedding+upserting docs whose source-file mtime matches.

These tests intercept fastembed via the existing module-level cache
helpers (`_reset_model_cache_for_tests` + a counting wrapper) so they:

  * never download fastembed model weights
  * never call out to the GPU/CPU embedder
  * deterministically assert HOW MANY docs were embedded vs skipped

The collection itself is a real embedded-Qdrant store in `isolated_xdg`
(per the existing test pattern) so we exercise the actual upsert path.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from recall import qdrant_backend
from recall.core import Document
from recall.qdrant_backend import (
    ensure_collection,
    upsert_documents,
    collection_mtimes,
    _qdrant_client_singleton,
)

pytestmark = pytest.mark.embeddings


_DENSE_DIM = qdrant_backend._DENSE_DIM


@pytest.fixture(autouse=True)
def _reset_qdrant_caches():
    """Hermetic: fresh client + model caches between tests."""
    qdrant_backend._reset_client_cache_for_tests()
    qdrant_backend._reset_model_cache_for_tests()
    yield
    qdrant_backend._reset_client_cache_for_tests()
    qdrant_backend._reset_model_cache_for_tests()


@pytest.fixture
def stub_embedders(monkeypatch):
    """Replace fastembed with deterministic counters.

    Returns a dict with:
        - `dense_calls`:  list of texts each call to dense.embed() received
        - `sparse_calls`: same for sparse
        - `total_embed_count()`: aggregate count
    """
    state = {"dense_calls": [], "sparse_calls": []}

    class _DenseStub:
        def embed(self, texts):
            texts = list(texts)
            state["dense_calls"].append(list(texts))
            return [[0.1] * _DENSE_DIM for _ in texts]

        def query_embed(self, texts):
            return [[0.1] * _DENSE_DIM for _ in list(texts)]

    class _SparseStub:
        def embed(self, texts):
            texts = list(texts)
            state["sparse_calls"].append(list(texts))
            for _ in texts:
                v = MagicMock()
                v.indices = [0]
                v.values = [0.1]
                yield v

        def query_embed(self, texts):
            for _ in list(texts):
                v = MagicMock()
                v.indices = [0]
                v.values = [0.1]
                yield v

    monkeypatch.setattr(qdrant_backend, "_get_embedder", lambda *a, **kw: _DenseStub())
    monkeypatch.setattr(qdrant_backend, "_get_sparse_embedder", lambda *a, **kw: _SparseStub())

    state["total_embed_count"] = lambda: sum(len(c) for c in state["dense_calls"])
    return state


def _doc(path: Path, body: str = "body content") -> Document:
    """Build a Document AND write the file (so os.stat works)."""
    path.write_text(body)
    return Document(
        path=str(path),
        source="test",
        title=path.stem,
        frontmatter={},
        body=body,
        text=body,
    )


@pytest.fixture
def fresh_client(isolated_xdg):
    """Embedded-Qdrant client in an isolated cache dir."""
    cache_dir = isolated_xdg / "xdg-cache" / "recall"
    cache_dir.mkdir(parents=True, exist_ok=True)
    client = _qdrant_client_singleton(cache_dir)
    ensure_collection(client, "test")
    yield client


class TestIncrementalEmbedding:
    def test_first_time_indexes_all_docs(
        self, tmp_path: Path, fresh_client, stub_embedders
    ):
        """Empty collection → every doc must be embedded."""
        docs = [_doc(tmp_path / f"doc{i}.md") for i in range(5)]
        n = upsert_documents(fresh_client, "test", docs)
        assert n == 5
        assert stub_embedders["total_embed_count"]() == 5

    def test_no_changes_skips_all(
        self, tmp_path: Path, fresh_client, stub_embedders
    ):
        """Re-running with unchanged docs+mtimes must embed ZERO."""
        docs = [_doc(tmp_path / f"doc{i}.md") for i in range(5)]
        upsert_documents(fresh_client, "test", docs)
        # Reset the call log so we measure ONLY the second call.
        stub_embedders["dense_calls"].clear()
        stub_embedders["sparse_calls"].clear()

        # Second run with same docs (unchanged mtimes).
        n2 = upsert_documents(fresh_client, "test", docs)
        assert n2 == 0, "second pass with no changes should write zero points"
        assert stub_embedders["total_embed_count"]() == 0, (
            "second pass must NOT call the embedder for unchanged docs"
        )

    def test_only_modified_doc_is_re_embedded(
        self, tmp_path: Path, fresh_client, stub_embedders
    ):
        """Touch one file → only that file should be embedded on the second pass."""
        docs = [_doc(tmp_path / f"doc{i}.md") for i in range(5)]
        upsert_documents(fresh_client, "test", docs)
        stub_embedders["dense_calls"].clear()

        # Modify doc2: bump its mtime via the filesystem.
        target = Path(docs[2].path)
        target.write_text("MODIFIED CONTENT")
        # Filesystems have varying mtime granularity; force a higher mtime.
        future = os.path.getmtime(target) + 10.0
        os.utime(target, (future, future))
        new_docs = [
            Document(
                path=d.path, source=d.source, title=d.title,
                frontmatter=d.frontmatter, body=d.body, text=d.text,
            )
            if d.path != docs[2].path
            else Document(
                path=docs[2].path, source="test", title="doc2",
                frontmatter={}, body="MODIFIED CONTENT", text="MODIFIED CONTENT",
            )
            for d in docs
        ]
        n = upsert_documents(fresh_client, "test", new_docs)
        assert n == 1, f"only the modified doc should be upserted, got {n}"
        assert stub_embedders["total_embed_count"]() == 1, (
            "embedder must be called exactly once (for the modified doc)"
        )

    def test_new_doc_added_only_embeds_new(
        self, tmp_path: Path, fresh_client, stub_embedders
    ):
        """Add a new file, leave others untouched → only new file embedded."""
        docs = [_doc(tmp_path / f"doc{i}.md") for i in range(3)]
        upsert_documents(fresh_client, "test", docs)
        stub_embedders["dense_calls"].clear()

        # Add a fourth doc.
        new_doc = _doc(tmp_path / "doc-new.md", body="brand new")
        docs.append(new_doc)
        n = upsert_documents(fresh_client, "test", docs)
        assert n == 1
        assert stub_embedders["total_embed_count"]() == 1

    def test_missing_source_file_treated_as_new(
        self, tmp_path: Path, fresh_client, stub_embedders
    ):
        """If a doc's source file is deleted but the Document is still passed,
        the upsert must not crash; the stored mtime of 0.0 sentinel triggers
        re-embedding (defensive — won't happen in practice since build_index
        constructs Documents from discovered files, but contract should be
        crash-free)."""
        doc = _doc(tmp_path / "doc.md")
        upsert_documents(fresh_client, "test", [doc])
        os.remove(doc.path)
        # Should not raise; either re-embeds (mtime=0 sentinel mismatches the
        # stored mtime) or skips, but never crashes.
        n = upsert_documents(fresh_client, "test", [doc])
        assert n in (0, 1)

    def test_skips_dont_affect_mtime_payload(
        self, tmp_path: Path, fresh_client, stub_embedders
    ):
        """Skipped docs keep their original mtime stored — no silent decay."""
        doc = _doc(tmp_path / "stable.md")
        upsert_documents(fresh_client, "test", [doc])
        mtime_after_first = collection_mtimes(fresh_client, "test")[doc.path]

        # Re-run, expect skip
        upsert_documents(fresh_client, "test", [doc])
        mtime_after_second = collection_mtimes(fresh_client, "test")[doc.path]

        assert mtime_after_first == mtime_after_second, (
            "skipping should NOT rewrite mtime"
        )


class TestPerformanceContract:
    def test_745_doc_brain_with_one_change_embeds_one(
        self, tmp_path: Path, fresh_client, stub_embedders
    ):
        """The real-world scenario this PR fixes: 745-doc brain, sync.sh
        touches 1 file, recall query triggers upsert — must embed 1 doc,
        not 745."""
        # Build a synthetic 50-doc corpus (scaled down for test speed; the
        # behavior is N-independent so 50 suffices to prove the contract).
        docs = [_doc(tmp_path / f"doc{i:03d}.md", body=f"content {i}") for i in range(50)]
        upsert_documents(fresh_client, "test", docs)
        stub_embedders["dense_calls"].clear()

        # Touch one file's content + mtime
        target = Path(docs[25].path)
        target.write_text("TOUCHED")
        future = os.path.getmtime(target) + 10.0
        os.utime(target, (future, future))
        new_docs = list(docs)
        new_docs[25] = Document(
            path=docs[25].path, source="test", title="doc025",
            frontmatter={}, body="TOUCHED", text="TOUCHED",
        )

        upsert_documents(fresh_client, "test", new_docs)
        assert stub_embedders["total_embed_count"]() == 1, (
            f"only 1 doc changed; embedder should be called once, "
            f"got {stub_embedders['total_embed_count']()} calls"
        )
