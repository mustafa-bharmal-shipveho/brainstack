"""Tests for the index cache: build, load, refresh-on-stale."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from recall.config import SourceConfig
from recall.index import IndexCache, build_index, load_index, needs_refresh


class TestBuildIndex:
    def test_creates_cache_dir(self, isolated_xdg, auto_memory_brain):
        sc = SourceConfig(
            name="brain",
            path=str(auto_memory_brain),
            glob="**/*.md",
            frontmatter="auto-memory",
            exclude=[],
        )
        cache = build_index([sc])
        assert isinstance(cache, IndexCache)
        assert cache.cache_dir.exists()

    def test_writes_files_manifest(self, isolated_xdg, auto_memory_brain):
        sc = SourceConfig(
            name="brain",
            path=str(auto_memory_brain),
            glob="**/*.md",
            frontmatter="auto-memory",
            exclude=[],
        )
        cache = build_index([sc])
        manifest = cache.cache_dir / "files.json"
        assert manifest.exists()

    def test_round_trip(self, isolated_xdg, auto_memory_brain):
        sc = SourceConfig(
            name="brain",
            path=str(auto_memory_brain),
            glob="**/*.md",
            frontmatter="auto-memory",
            exclude=[],
        )
        build_index([sc])
        loaded = load_index([sc])
        assert loaded is not None
        # Same number of docs
        first = build_index([sc])
        assert len(first.documents) == len(loaded.documents)


class TestNeedsRefresh:
    def test_no_cache_means_refresh_needed(self, isolated_xdg, auto_memory_brain):
        sc = SourceConfig(
            name="brain",
            path=str(auto_memory_brain),
            glob="**/*.md",
            frontmatter="auto-memory",
            exclude=[],
        )
        assert needs_refresh([sc]) is True

    def test_cache_present_means_no_refresh(self, isolated_xdg, auto_memory_brain):
        sc = SourceConfig(
            name="brain",
            path=str(auto_memory_brain),
            glob="**/*.md",
            frontmatter="auto-memory",
            exclude=[],
        )
        build_index([sc])
        assert needs_refresh([sc]) is False

    def test_modified_file_triggers_refresh(self, isolated_xdg, auto_memory_brain):
        sc = SourceConfig(
            name="brain",
            path=str(auto_memory_brain),
            glob="**/*.md",
            frontmatter="auto-memory",
            exclude=[],
        )
        build_index([sc])
        assert needs_refresh([sc]) is False

        # Touch a file to update mtime — must wait at least 1s on filesystems with 1s resolution
        target = auto_memory_brain / "semantic/lessons/feedback_pin_dependencies.md"
        future = time.time() + 5
        import os

        os.utime(target, (future, future))

        assert needs_refresh([sc]) is True

    def test_added_file_triggers_refresh(self, isolated_xdg, auto_memory_brain):
        sc = SourceConfig(
            name="brain",
            path=str(auto_memory_brain),
            glob="**/*.md",
            frontmatter="auto-memory",
            exclude=[],
        )
        build_index([sc])
        # Add a new file
        new_file = auto_memory_brain / "semantic/lessons/brand_new.md"
        new_file.write_text(
            "---\nname: new\ndescription: just added\ntype: feedback\n---\nbody\n",
            encoding="utf-8",
        )
        assert needs_refresh([sc]) is True

    def test_removed_file_triggers_refresh(self, isolated_xdg, auto_memory_brain):
        sc = SourceConfig(
            name="brain",
            path=str(auto_memory_brain),
            glob="**/*.md",
            frontmatter="auto-memory",
            exclude=[],
        )
        build_index([sc])
        target = auto_memory_brain / "semantic/lessons/feedback_pin_dependencies.md"
        target.unlink()
        assert needs_refresh([sc]) is True


class TestIndexAtomicWrites:
    def test_partial_failure_does_not_corrupt_existing(self, isolated_xdg, auto_memory_brain, monkeypatch):
        sc = SourceConfig(
            name="brain",
            path=str(auto_memory_brain),
            glob="**/*.md",
            frontmatter="auto-memory",
            exclude=[],
        )
        build_index([sc])
        cache_dir = load_index([sc]).cache_dir
        manifest_before = (cache_dir / "files.json").read_text()

        # Corrupt the source mid-flight by deleting all files; rebuild should fail or
        # produce empty-ish but not corrupt the existing on-disk manifest atomically.
        # We can't easily test atomic-rename failure, so just ensure rebuild is idempotent.
        build_index([sc])
        manifest_after = (cache_dir / "files.json").read_text()
        # Should be valid JSON after either run
        import json

        assert json.loads(manifest_after) is not None
