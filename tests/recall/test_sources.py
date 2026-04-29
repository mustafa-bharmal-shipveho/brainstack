"""Tests for source plugins (file discovery + frontmatter handling)."""

from __future__ import annotations

from pathlib import Path

import pytest

from recall.config import SourceConfig
from recall.sources import Document, discover_documents


class TestAutoMemorySource:
    def test_finds_all_files(self, auto_memory_brain):
        sc = SourceConfig(
            name="brain",
            path=str(auto_memory_brain),
            glob="**/*.md",
            frontmatter="auto-memory",
            exclude=["episodic/**", "candidates/**", "working/**", "scripts/**"],
        )
        docs = list(discover_documents(sc))
        # 8 entries written + MEMORY.md = 9
        assert len(docs) == 9

    def test_parses_frontmatter(self, auto_memory_brain):
        sc = SourceConfig(
            name="brain",
            path=str(auto_memory_brain),
            glob="**/*.md",
            frontmatter="auto-memory",
            exclude=[],
        )
        docs = list(discover_documents(sc))
        names = {d.frontmatter.get("name") for d in docs if d.frontmatter}
        assert "pin-dependencies" in names
        assert "atomic-writes" in names

    def test_exclude_patterns_work(self, auto_memory_brain):
        # add an episodic dir
        (auto_memory_brain / "episodic").mkdir()
        (auto_memory_brain / "episodic" / "raw.md").write_text("# raw\n", encoding="utf-8")

        sc = SourceConfig(
            name="brain",
            path=str(auto_memory_brain),
            glob="**/*.md",
            frontmatter="auto-memory",
            exclude=["episodic/**"],
        )
        docs = list(discover_documents(sc))
        paths = [str(d.path) for d in docs]
        assert not any("episodic" in p for p in paths)

    def test_assigns_source_name(self, auto_memory_brain):
        sc = SourceConfig(
            name="my-brain-name",
            path=str(auto_memory_brain),
            glob="**/*.md",
            frontmatter="auto-memory",
            exclude=[],
        )
        docs = list(discover_documents(sc))
        assert all(d.source == "my-brain-name" for d in docs)


class TestGenericSource:
    def test_finds_files_without_frontmatter(self, generic_brain):
        sc = SourceConfig(
            name="vault",
            path=str(generic_brain),
            glob="**/*.md",
            frontmatter="optional",
            exclude=[],
        )
        docs = list(discover_documents(sc))
        assert len(docs) == len(_count_md(generic_brain))

    def test_first_h1_used_when_no_frontmatter(self, generic_brain):
        sc = SourceConfig(
            name="vault",
            path=str(generic_brain),
            glob="**/*.md",
            frontmatter="optional",
            exclude=[],
        )
        docs = list(discover_documents(sc))
        lasagna = [d for d in docs if Path(d.path).name == "lasagna.md"][0]
        # When no frontmatter, the title falls back to the H1
        assert lasagna.title and "Lasagna" in lasagna.title

    def test_optional_frontmatter_parsed_when_present(self, generic_brain):
        sc = SourceConfig(
            name="vault",
            path=str(generic_brain),
            glob="**/*.md",
            frontmatter="optional",
            exclude=[],
        )
        docs = list(discover_documents(sc))
        scales = [d for d in docs if Path(d.path).name == "scales.md"][0]
        # scales.md has frontmatter with name + tags
        assert scales.frontmatter.get("name") == "scales"

    def test_recurses_deeply_nested(self, generic_brain):
        sc = SourceConfig(
            name="vault",
            path=str(generic_brain),
            glob="**/*.md",
            frontmatter="optional",
            exclude=[],
        )
        docs = list(discover_documents(sc))
        # Use endswith to avoid matching the tmp_path which may contain "deeply"
        deep = [
            d
            for d in docs
            if str(d.path).replace("\\", "/").endswith("deeply/nested/path/note.md")
        ]
        assert len(deep) == 1


class TestEdgeCases:
    def test_empty_brain_returns_empty(self, empty_brain):
        sc = SourceConfig(
            name="empty",
            path=str(empty_brain),
            glob="**/*.md",
            frontmatter="optional",
            exclude=[],
        )
        assert list(discover_documents(sc)) == []

    def test_single_file_brain(self, single_file_brain):
        sc = SourceConfig(
            name="solo",
            path=str(single_file_brain),
            glob="**/*.md",
            frontmatter="auto-memory",
            exclude=[],
        )
        docs = list(discover_documents(sc))
        assert len(docs) == 1
        assert docs[0].frontmatter["name"] == "lone"

    def test_malformed_files_dont_crash(self, malformed_brain):
        sc = SourceConfig(
            name="broken",
            path=str(malformed_brain),
            glob="**/*.md",
            frontmatter="optional",
            exclude=[],
        )
        # Should yield Documents for every .md file, even malformed ones
        docs = list(discover_documents(sc))
        # Empty + binary may be skipped; others should parse with empty frontmatter
        assert len(docs) >= 5

    def test_nonexistent_path_raises_or_yields_nothing(self, tmp_path):
        sc = SourceConfig(
            name="ghost",
            path=str(tmp_path / "does-not-exist"),
            glob="**/*.md",
            frontmatter="optional",
            exclude=[],
        )
        # Either raise FileNotFoundError or yield nothing — both acceptable
        try:
            docs = list(discover_documents(sc))
            assert docs == []
        except FileNotFoundError:
            pass

    def test_symlink_to_self_is_safe(self, tmp_path):
        brain = tmp_path / "loopy"
        brain.mkdir()
        (brain / "a.md").write_text("# A\n", encoding="utf-8")
        # Create a symlink loop: brain/sub -> brain
        try:
            (brain / "sub").symlink_to(brain)
        except (OSError, NotImplementedError):
            pytest.skip("Cannot create symlinks on this platform")

        sc = SourceConfig(
            name="loopy",
            path=str(brain),
            glob="**/*.md",
            frontmatter="optional",
            exclude=[],
        )
        # Must terminate; result is implementation-defined but must not loop forever
        docs = list(discover_documents(sc))
        assert len(docs) >= 1


def _count_md(path: Path) -> list[Path]:
    return [p for p in path.rglob("*.md")]
