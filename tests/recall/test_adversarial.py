"""Adversarial input tests — pathological cases that should NOT crash or
silently corrupt state.

Each test corresponds to a bug found during the overnight bug-hunt phase. If
one of these fails, a real safety/correctness issue has regressed.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path

import pytest
import yaml

from recall.config import Config, SourceConfig
from recall.core import Bm25Retriever, Document, HybridRetriever
from recall.frontmatter import parse_file_text, parse_path
from recall.index import build_index, needs_refresh
from recall.migrate import MigrationAbort, plan_migration
from recall.sources import _matches_any, discover_documents


# ---------------------------------------------------------------------------
# Migration safety
# ---------------------------------------------------------------------------


class TestMigrationDestructiveCases:
    def test_target_equals_source_aborts(self, tmp_path):
        brain = tmp_path / "brain"
        brain.mkdir()
        (brain / "x.md").write_text("hi", encoding="utf-8")
        with pytest.raises(MigrationAbort, match="cannot equal"):
            plan_migration(source=brain, target=brain)

    def test_target_inside_source_aborts(self, tmp_path):
        brain = tmp_path / "brain"
        brain.mkdir()
        (brain / "x.md").write_text("hi", encoding="utf-8")
        target = brain / "subdir"
        with pytest.raises(MigrationAbort, match="inside"):
            plan_migration(source=brain, target=target)

    def test_source_inside_target_aborts(self, tmp_path):
        target = tmp_path / "target"
        target.mkdir()
        source = target / "inner"
        source.mkdir()
        (source / "x.md").write_text("hi", encoding="utf-8")
        with pytest.raises(MigrationAbort, match="inside"):
            plan_migration(source=source, target=target)

    def test_source_is_a_file_aborts(self, tmp_path):
        f = tmp_path / "not-a-dir.md"
        f.write_text("hi", encoding="utf-8")
        with pytest.raises(MigrationAbort, match="not a directory"):
            plan_migration(source=f, target=tmp_path / "target")


# ---------------------------------------------------------------------------
# Cache poisoning — needs_refresh must not crash on garbage manifest
# ---------------------------------------------------------------------------


class TestCachePoisoning:
    def test_null_manifest_triggers_refresh(self, isolated_xdg, auto_memory_brain):
        sc = SourceConfig(
            name="brain",
            path=str(auto_memory_brain),
            glob="**/*.md",
            frontmatter="auto-memory",
            exclude=[],
        )
        # Build first so the cache dir exists, then poison the manifest.
        build_index([sc])
        from recall.config import cache_dir

        manifest = cache_dir() / "files.json"
        manifest.write_text("null", encoding="utf-8")
        # Should not raise; should report refresh needed.
        assert needs_refresh([sc]) is True

    def test_non_list_sources_triggers_refresh(self, isolated_xdg, auto_memory_brain):
        sc = SourceConfig(
            name="brain",
            path=str(auto_memory_brain),
            glob="**/*.md",
            frontmatter="auto-memory",
            exclude=[],
        )
        build_index([sc])
        from recall.config import cache_dir

        manifest = cache_dir() / "files.json"
        manifest.write_text(json.dumps({"sources": "not a list"}), encoding="utf-8")
        assert needs_refresh([sc]) is True

    def test_top_level_string_manifest_triggers_refresh(self, isolated_xdg, auto_memory_brain):
        sc = SourceConfig(
            name="brain",
            path=str(auto_memory_brain),
            glob="**/*.md",
            frontmatter="auto-memory",
            exclude=[],
        )
        build_index([sc])
        from recall.config import cache_dir

        manifest = cache_dir() / "files.json"
        manifest.write_text('"just a string"', encoding="utf-8")
        assert needs_refresh([sc]) is True

    def test_huge_manifest_does_not_oom(self, isolated_xdg, auto_memory_brain):
        """A huge but well-formed manifest should be detected as stale and rebuilt
        without OOM. We use ~100k entries — well within memory but enough to flag
        any quadratic loops."""
        sc = SourceConfig(
            name="brain",
            path=str(auto_memory_brain),
            glob="**/*.md",
            frontmatter="auto-memory",
            exclude=[],
        )
        build_index([sc])
        from recall.config import cache_dir

        manifest = cache_dir() / "files.json"
        # Lots of fake file entries — should be treated as stale (real fs has fewer).
        fake_entries = [
            {"path": f"/fake/{i}.md", "mtime": float(i)} for i in range(100_000)
        ]
        manifest.write_text(
            json.dumps({"sources": [{"source": sc.name, "path": sc.path, "files": fake_entries}]}),
            encoding="utf-8",
        )
        # Should run without OOM. Result should be True (stale).
        assert needs_refresh([sc]) is True


# ---------------------------------------------------------------------------
# YAML attack surface
# ---------------------------------------------------------------------------


class TestYamlAttacks:
    def test_python_object_tag_does_not_execute(self):
        """yaml.safe_load already blocks this. Verify the wrapper doesn't undo it."""
        # !!python/object would let an attacker run __reduce__. With safe_load,
        # this raises. Our parser must catch and degrade.
        text = "---\nname: !!python/object/apply:os.system [\"echo pwned\"]\n---\n"
        parsed = parse_file_text(text)
        # Parser must NOT crash. Either frontmatter is empty (preferred) or
        # the malicious value is left untouched (no execution).
        assert isinstance(parsed.frontmatter, dict)

    def test_billion_laughs_anchor_expansion_is_bounded(self):
        """A YAML document with deeply nested anchors should be rejected or
        truncated rather than exploding to gigabytes."""
        # Construct an exponential-anchor YAML
        bomb = "---\n"
        bomb += "a: &a [x, x, x, x, x, x, x, x, x, x]\n"
        bomb += "b: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a, *a]\n"
        bomb += "c: &c [*b, *b, *b, *b, *b, *b, *b, *b, *b, *b]\n"
        bomb += "d: &d [*c, *c, *c, *c, *c, *c, *c, *c, *c, *c]\n"
        bomb += "---\nbody\n"
        # Should complete in bounded time without OOM. We give a generous
        # 5-second wall clock — anything more indicates an unbounded expansion.
        start = time.time()
        parsed = parse_file_text(bomb)
        elapsed = time.time() - start
        assert elapsed < 5.0, f"YAML parsing took {elapsed:.1f}s — possible bomb expansion"
        assert isinstance(parsed.frontmatter, dict)

    def test_giant_frontmatter_value_is_bounded(self):
        """A 5 MB value should be parsed quickly (or rejected) — not freeze."""
        giant = "x" * (5 * 1024 * 1024)
        text = f"---\nname: huge\ndescription: {giant}\n---\nbody\n"
        start = time.time()
        parsed = parse_file_text(text)
        elapsed = time.time() - start
        assert elapsed < 5.0, f"Parsing 5MB value took {elapsed:.1f}s"
        # If it parsed, the value should be present
        if "description" in parsed.frontmatter:
            assert isinstance(parsed.frontmatter["description"], str)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_empty_path_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="empty"):
            SourceConfig(
                name="bad",
                path="",
                glob="**/*.md",
                frontmatter="optional",
                exclude=[],
            )


# ---------------------------------------------------------------------------
# Retriever edge cases
# ---------------------------------------------------------------------------


class TestRetrieverEdgeCases:
    def _doc(self, name: str, text: str) -> Document:
        return Document(
            path=f"/synth/{name}.md",
            source="brain",
            title=name,
            frontmatter={"name": name, "description": text, "type": "reference"},
            body="",
            text=text,
        )

    def test_bm25_k_zero_returns_empty(self):
        retriever = Bm25Retriever([self._doc("a", "hello"), self._doc("b", "world")])
        assert retriever.query("hello", k=0) == []

    def test_bm25_negative_k_returns_empty(self):
        retriever = Bm25Retriever([self._doc("a", "hello")])
        assert retriever.query("hello", k=-1) == []

    def test_bm25_k_huge_returns_all(self):
        docs = [self._doc(f"d{i}", f"text{i}") for i in range(5)]
        retriever = Bm25Retriever(docs)
        assert len(retriever.query("text0", k=1_000_000)) == 5

    def test_hybrid_k_zero(self):
        retriever = HybridRetriever(
            [self._doc("a", "hello world")],
            embedding_weight=0.0,
        )
        assert retriever.query("hello", k=0) == []


# ---------------------------------------------------------------------------
# Glob / exclude pattern edge cases
# ---------------------------------------------------------------------------


class TestExcludeMatching:
    def test_triple_star_glob_does_not_crash(self):
        # ***/*.md is malformed but should not raise
        assert _matches_any("foo/bar.md", ["***/*.md"]) in (True, False)

    def test_empty_pattern_list(self):
        assert _matches_any("any/path.md", []) is False

    def test_trailing_slash_double_star(self):
        # `dir/**` should match `dir/anything.md` and deeper
        assert _matches_any("episodic/raw.md", ["episodic/**"])
        assert _matches_any("episodic/sub/deep.md", ["episodic/**"])
        assert _matches_any("episodic", ["episodic/**"])
        # but should not match a different dir
        assert not _matches_any("semantic/raw.md", ["episodic/**"])


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


class TestGlobConfig:
    """The `glob` field in SourceConfig must actually be applied."""

    def test_glob_only_md_below_subdir(self, tmp_path):
        brain = tmp_path / "brain"
        (brain / "include").mkdir(parents=True)
        (brain / "exclude").mkdir(parents=True)
        (brain / "include" / "a.md").write_text("---\nname: a\n---\n", encoding="utf-8")
        (brain / "exclude" / "b.md").write_text("---\nname: b\n---\n", encoding="utf-8")
        sc = SourceConfig(
            name="brain",
            path=str(brain),
            glob="include/**/*.md",
            frontmatter="auto-memory",
            exclude=[],
        )
        docs = list(discover_documents(sc))
        names = {d.frontmatter.get("name") for d in docs}
        assert "a" in names
        assert "b" not in names

    def test_glob_specific_extension(self, tmp_path):
        brain = tmp_path / "brain"
        brain.mkdir()
        (brain / "real.md").write_text("---\nname: real\n---\n", encoding="utf-8")
        (brain / "decoy.txt").write_text("---\nname: decoy\n---\n", encoding="utf-8")
        sc = SourceConfig(
            name="brain",
            path=str(brain),
            glob="**/*.md",
            frontmatter="auto-memory",
            exclude=[],
        )
        docs = list(discover_documents(sc))
        names = {d.frontmatter.get("name") for d in docs}
        assert names == {"real"}


class TestSymlinkContainment:
    """Symlinks must not allow reading files outside the configured root."""

    def test_symlink_escape_blocked(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.md"
        secret.write_text("---\nname: secret\ndescription: should not be indexed\n---\n", encoding="utf-8")

        brain = tmp_path / "brain"
        brain.mkdir()
        try:
            (brain / "leaked.md").symlink_to(secret)
        except (OSError, NotImplementedError):
            pytest.skip("Cannot create symlinks on this platform")

        sc = SourceConfig(
            name="brain",
            path=str(brain),
            glob="**/*.md",
            frontmatter="auto-memory",
            exclude=[],
        )
        docs = list(discover_documents(sc))
        # The escaping symlink must NOT be followed; the secret must not appear
        names = {d.frontmatter.get("name") for d in docs}
        assert "secret" not in names


class TestCachePathTraversal:
    def test_dotdot_in_source_name_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="invalid source name"):
            SourceConfig(
                name="../escape",
                path=str(tmp_path),
                glob="**/*.md",
                frontmatter="optional",
                exclude=[],
            )

    def test_slash_in_source_name_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="invalid source name"):
            SourceConfig(
                name="a/b",
                path=str(tmp_path),
                glob="**/*.md",
                frontmatter="optional",
                exclude=[],
            )

    def test_absolute_path_in_source_name_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="invalid source name"):
            SourceConfig(
                name="/abs",
                path=str(tmp_path),
                glob="**/*.md",
                frontmatter="optional",
                exclude=[],
            )


class TestJsonSerialization:
    """Frontmatter values that aren't directly JSON-serializable (dates,
    times) must be coerced to strings in CLI/MCP output."""

    def test_date_value_is_serialized(self, tmp_path):
        from recall.sources import discover_documents

        brain = tmp_path / "brain"
        brain.mkdir()
        # YAML interprets ISO-format strings as date objects via safe_load
        (brain / "dated.md").write_text(
            "---\nname: dated\ndescription: has a date\ndate: 2026-04-28\ntype: feedback\n---\nbody\n",
            encoding="utf-8",
        )
        sc = SourceConfig(
            name="brain",
            path=str(brain),
            glob="**/*.md",
            frontmatter="auto-memory",
            exclude=[],
        )
        docs = list(discover_documents(sc))
        retriever = HybridRetriever(docs, embedding_weight=0.0)
        results = retriever.query("date", k=1)

        # Use the same serializer the CLI uses
        from recall.serialize import serialize_results

        out = serialize_results(results)
        # MUST be JSON-encodable end-to-end
        json.dumps(out)


class TestConcurrentReindex:
    def test_two_concurrent_builds_dont_corrupt_manifest(
        self, isolated_xdg, auto_memory_brain
    ):
        """Two threads calling build_index simultaneously must not produce a
        corrupt manifest. Either one wins or they merge; the resulting JSON
        must always be parseable."""
        sc = SourceConfig(
            name="brain",
            path=str(auto_memory_brain),
            glob="**/*.md",
            frontmatter="auto-memory",
            exclude=[],
        )

        errors: list[Exception] = []

        def runner():
            try:
                for _ in range(3):
                    build_index([sc])
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=runner) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        from recall.config import cache_dir

        manifest = cache_dir() / "files.json"
        # At minimum: the manifest exists and parses
        assert manifest.exists(), errors
        loaded = json.loads(manifest.read_text(encoding="utf-8"))
        assert "sources" in loaded
        assert errors == [], f"Build raised errors under concurrency: {errors[:3]}"
