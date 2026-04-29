"""Tests for the Qdrant-backed index lifecycle."""

from __future__ import annotations

import os
import time

import pytest

from recall.config import SourceConfig
from recall.index import IndexCache, build_index, load_index, needs_refresh


@pytest.fixture(autouse=True)
def _reset_qdrant():
    from recall import qdrant_backend as qb

    qb._reset_client_cache_for_tests()
    yield
    qb._reset_client_cache_for_tests()


def _src(brain) -> SourceConfig:
    return SourceConfig(
        name="brain",
        path=str(brain),
        glob="**/*.md",
        frontmatter="auto-memory",
        exclude=[],
    )


class TestBuildIndex:
    def test_creates_qdrant_collection_with_points(self, isolated_xdg, auto_memory_brain):
        from recall import qdrant_backend as qb
        from recall.config import cache_dir

        sc = _src(auto_memory_brain)
        cache = build_index([sc])
        assert isinstance(cache, IndexCache)
        client = qb._qdrant_client_singleton(cache_dir())
        assert client.collection_exists("brain")
        assert qb.count(client, "brain") == len(cache.documents)
        assert qb.count(client, "brain") > 0

    def test_idempotent_rebuild_no_duplicates(self, isolated_xdg, auto_memory_brain):
        from recall import qdrant_backend as qb
        from recall.config import cache_dir

        sc = _src(auto_memory_brain)
        first = build_index([sc])
        second = build_index([sc])
        client = qb._qdrant_client_singleton(cache_dir())
        # Same FS state → same point count thanks to deterministic ids
        assert qb.count(client, "brain") == len(first.documents) == len(second.documents)


class TestNeedsRefresh:
    def test_no_collection_means_refresh_needed(self, isolated_xdg, auto_memory_brain):
        assert needs_refresh([_src(auto_memory_brain)]) is True

    def test_after_build_no_refresh(self, isolated_xdg, auto_memory_brain):
        sc = _src(auto_memory_brain)
        build_index([sc])
        assert needs_refresh([sc]) is False

    def test_mtime_change_triggers_refresh(self, isolated_xdg, auto_memory_brain):
        sc = _src(auto_memory_brain)
        build_index([sc])
        # Touch one of the files
        target = next(auto_memory_brain.rglob("*.md"))
        future = time.time() + 5
        os.utime(target, (future, future))
        assert needs_refresh([sc]) is True

    def test_added_file_triggers_refresh(self, isolated_xdg, auto_memory_brain):
        sc = _src(auto_memory_brain)
        build_index([sc])
        new = auto_memory_brain / "brand_new.md"
        new.write_text(
            "---\nname: new\ndescription: just added\ntype: feedback\n---\nbody\n",
            encoding="utf-8",
        )
        assert needs_refresh([sc]) is True

    def test_removed_file_triggers_refresh(self, isolated_xdg, auto_memory_brain):
        sc = _src(auto_memory_brain)
        build_index([sc])
        # Remove one file
        target = next(auto_memory_brain.rglob("*.md"))
        target.unlink()
        assert needs_refresh([sc]) is True


class TestLegacyCleanup:
    def test_first_new_build_removes_legacy_manifest(
        self, isolated_xdg, auto_memory_brain
    ):
        from recall.config import cache_dir

        base = cache_dir()
        base.mkdir(parents=True, exist_ok=True)
        legacy_top = base / "files.json"
        legacy_top.write_text('{"sources": []}', encoding="utf-8")
        per_source = base / "brain" / "files.json"
        per_source.parent.mkdir(parents=True, exist_ok=True)
        per_source.write_text('{"source": "brain", "files": []}', encoding="utf-8")

        build_index([_src(auto_memory_brain)])
        assert not legacy_top.exists()
        assert not per_source.exists()


class TestLoadIndex:
    def test_load_returns_none_when_no_data(self, isolated_xdg, auto_memory_brain):
        # Collection has never been built
        assert load_index([_src(auto_memory_brain)]) is None

    def test_load_after_build_returns_documents(self, isolated_xdg, auto_memory_brain):
        sc = _src(auto_memory_brain)
        build_index([sc])
        loaded = load_index([sc])
        assert loaded is not None
        assert len(loaded.documents) > 0
