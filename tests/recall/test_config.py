"""Tests for config loading and BRAIN_HOME / XDG resolution."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from recall.config import (
    Config,
    SourceConfig,
    _MIGRATION_MARKER_KEY,
    _MIGRATION_MARKER_V2,
    config_path,
    default_config,
    load_config,
    resolve_brain_home,
)


class TestResolveBrainHome:
    def test_explicit_env(self, isolated_xdg, monkeypatch, tmp_path):
        target = tmp_path / "custom-brain"
        monkeypatch.setenv("BRAIN_HOME", str(target))
        assert resolve_brain_home() == target

    def test_falls_back_to_xdg_data(self, isolated_xdg, monkeypatch):
        monkeypatch.delenv("BRAIN_HOME", raising=False)
        result = resolve_brain_home()
        assert result.name == "brain"
        assert "xdg-data" in str(result)

    def test_expands_tilde(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BRAIN_HOME", "~/somewhere/brain")
        monkeypatch.setenv("HOME", str(tmp_path))
        result = resolve_brain_home()
        assert "~" not in str(result)

    def test_expands_env_var(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CUSTOM_BASE", str(tmp_path / "base"))
        monkeypatch.setenv("BRAIN_HOME", "$CUSTOM_BASE/brain")
        result = resolve_brain_home()
        assert "$CUSTOM_BASE" not in str(result)
        assert str(tmp_path / "base" / "brain") == str(result)


class TestConfigPath:
    def test_uses_xdg_config_home(self, isolated_xdg):
        path = config_path()
        assert path.name == "config.json"
        assert path.parent.name == "recall"
        assert "xdg-config" in str(path)

    def test_falls_back_to_dot_config(self, monkeypatch, tmp_path):
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        path = config_path()
        # Either ~/.config/recall/config.json or platform default
        assert path.name == "config.json"
        assert path.parent.name == "recall"


class TestDefaultConfig:
    def test_returns_brain_and_imports_sources(self, isolated_xdg):
        """Default config indexes BOTH memory/ and imports/ so external
        folders registered via `--add-source` are retrievable without
        manual config edits. Ordering matters: brain first (it's the
        canonical content), imports second (mirror tier)."""
        cfg = default_config()
        assert len(cfg.sources) == 2
        assert [s.name for s in cfg.sources] == ["brain", "imports"]
        for s in cfg.sources:
            assert s.frontmatter == "auto-memory"

    def test_brain_excludes_episodic_and_working(self, isolated_xdg):
        cfg = default_config()
        brain_excludes = cfg.sources[0].exclude
        assert any("episodic" in e for e in brain_excludes)
        assert any("working" in e for e in brain_excludes)
        assert any("scripts" in e for e in brain_excludes)

    def test_imports_excludes_non_markdown_and_caches(self, isolated_xdg):
        """Imports tier mirrors raw tool memory + KBs; we want only .md
        retrievable. JSONL/JSON/TXT cover Claude session logs + Codex
        sessions + sidecars; .imported_misc.jsonl is the misc-adapter
        bookkeeping file (large, no retrieval value)."""
        cfg = default_config()
        imports_excludes = cfg.sources[1].exclude
        assert any("__pycache__" in e for e in imports_excludes)
        assert "*.json" in imports_excludes
        assert "*.jsonl" in imports_excludes
        assert "*.txt" in imports_excludes
        assert ".imported_misc.jsonl" in imports_excludes

    def test_default_k_is_5(self, isolated_xdg):
        cfg = default_config()
        assert cfg.default_k == 5


class TestImportsMigration:
    """Existing brains have a single-source config (`brain` only) written
    before this feature shipped. On next load_config(), they should auto-
    upgrade to include `imports` — but ONLY if the existing config still
    matches the original defaults. Customized configs are left alone.

    Marker contract: top-level `migration_marker = "v2-imports-source"`
    is written when migration runs (or when a fresh default_config is
    saved), making future load_config calls a no-op.
    """

    def test_legacy_single_brain_config_gets_imports_appended(
        self, isolated_xdg, write_config, monkeypatch
    ):
        """A user with a $BRAIN_ROOT/memory single source from before
        this feature → `imports` source added; marker stamped; file on
        disk now has both sources, with the imports path literal mirroring
        the brain path style ($BRAIN_ROOT/imports — portable across
        machines, not a resolved absolute path)."""
        # Mimic the real legacy-write scenario: BRAIN_ROOT set, BRAIN_HOME
        # NOT — that's what produces "$BRAIN_ROOT/memory" in the original
        # config. The isolated_xdg fixture sets both for hermeticity, so
        # we have to clear BRAIN_HOME explicitly here.
        monkeypatch.delenv("BRAIN_HOME", raising=False)
        monkeypatch.setenv("BRAIN_ROOT", str(isolated_xdg / "agent"))
        cfg_path = write_config(
            sources=[
                {
                    "name": "brain",
                    "path": "$BRAIN_ROOT/memory",
                    "glob": "**/*.md",
                    "frontmatter": "auto-memory",
                    "exclude": [
                        "episodic/**",
                        "candidates/**",
                        "working/**",
                        "scripts/**",
                        "__pycache__/**",
                        "MEMORY.md",
                        "semantic/LESSONS.md",
                    ],
                }
            ]
        )

        # Act
        cfg = load_config()

        # In-memory: 2 sources
        assert [s.name for s in cfg.sources] == ["brain", "imports"]

        # On disk: file persisted with marker + imports source
        on_disk = json.loads(cfg_path.read_text())
        assert on_disk.get(_MIGRATION_MARKER_KEY) == _MIGRATION_MARKER_V2
        assert [s["name"] for s in on_disk["sources"]] == ["brain", "imports"]
        # Imports source uses the canonical $BRAIN_ROOT literal — not a
        # resolved absolute path, so the config stays portable across
        # machines with different $HOME / $BRAIN_ROOT
        assert on_disk["sources"][1]["path"] == "$BRAIN_ROOT/imports"

    def test_migration_path_unaffected_by_current_env(
        self, isolated_xdg, write_config, monkeypatch
    ):
        """Migration must derive the imports path from the EXISTING brain
        path style (preserve `$BRAIN_ROOT/imports` if brain uses
        `$BRAIN_ROOT/memory`), not from whatever env the migration shell
        happens to have. Without this, a user running `recall sources`
        from a shell where BRAIN_ROOT isn't exported would migrate to a
        resolved XDG path while their brain source stayed as a literal."""
        # Original config written when BRAIN_ROOT was set
        write_config(
            sources=[
                {
                    "name": "brain",
                    "path": "$BRAIN_ROOT/memory",
                    "glob": "**/*.md",
                    "frontmatter": "auto-memory",
                    "exclude": [],
                }
            ]
        )
        # Migration runs in a shell where BRAIN_ROOT is unset (the bug case)
        monkeypatch.delenv("BRAIN_ROOT", raising=False)
        # BRAIN_HOME is also unset — without the literal-preservation logic,
        # _default_imports_path_literal() would fall through to the resolved
        # XDG path, mismatching the brain source's literal style
        monkeypatch.delenv("BRAIN_HOME", raising=False)
        load_config()
        on_disk = json.loads((isolated_xdg / "xdg-config" / "recall" / "config.json").read_text())
        # Imports source must mirror the brain literal, not be resolved
        assert on_disk["sources"][1]["path"] == "$BRAIN_ROOT/imports"

    def test_brain_home_legacy_config_also_migrates(
        self, isolated_xdg, write_config, monkeypatch
    ):
        """A user whose original config used $BRAIN_HOME (no $BRAIN_ROOT
        set at write time) should also migrate. Path-equality check must
        accept all three forms _default_brain_path_literal() can emit:
        $BRAIN_ROOT/memory, $BRAIN_HOME, or a resolved XDG path."""
        monkeypatch.delenv("BRAIN_ROOT", raising=False)
        write_config(
            sources=[
                {
                    "name": "brain",
                    "path": "$BRAIN_HOME",
                    "glob": "**/*.md",
                    "frontmatter": "auto-memory",
                    "exclude": ["episodic/**"],
                }
            ]
        )
        cfg = load_config()
        assert [s.name for s in cfg.sources] == ["brain", "imports"]

    def test_migration_is_idempotent(
        self, isolated_xdg, write_config, monkeypatch
    ):
        """Once the marker is stamped, subsequent load_config calls are
        a no-op — the source list stays at 2, the file isn't repeatedly
        rewritten with extra `imports` entries."""
        monkeypatch.setenv("BRAIN_ROOT", str(isolated_xdg / "agent"))
        cfg_path = write_config(
            sources=[
                {
                    "name": "brain",
                    "path": "$BRAIN_ROOT/memory",
                    "glob": "**/*.md",
                    "frontmatter": "auto-memory",
                    "exclude": ["episodic/**"],
                }
            ]
        )
        load_config()
        first = cfg_path.read_text()
        for _ in range(3):
            cfg = load_config()
        second = cfg_path.read_text()
        assert first == second
        assert len(cfg.sources) == 2

    def test_user_customized_path_skips_migration(
        self, isolated_xdg, write_config, monkeypatch
    ):
        """If the user pointed `brain` somewhere non-default (e.g.
        ~/my-notes), respect their intent — don't auto-add `imports`.
        They can do it themselves."""
        monkeypatch.setenv("BRAIN_ROOT", str(isolated_xdg / "agent"))
        cfg_path = write_config(
            sources=[
                {
                    "name": "brain",
                    "path": str(isolated_xdg / "my-notes"),
                    "glob": "**/*.md",
                    "frontmatter": "auto-memory",
                    "exclude": [],
                }
            ]
        )
        cfg = load_config()
        # No migration: still single source
        assert len(cfg.sources) == 1
        assert cfg.sources[0].name == "brain"
        # And no marker stamped — user's config untouched
        on_disk = json.loads(cfg_path.read_text())
        assert _MIGRATION_MARKER_KEY not in on_disk

    def test_user_brain_home_override_with_resolved_literal_skipped(
        self, isolated_xdg, write_config, monkeypatch
    ):
        """User has $BRAIN_HOME=/custom/vault AND wrote `path: "/custom/vault"`
        as a literal absolute. Resolved value matches the env value, but
        that's coincidence — the user's literal is intentional customization,
        not a default. Migration must skip. Codex 2026-05-05 P2.
        """
        custom_vault = isolated_xdg / "custom-vault"
        custom_vault.mkdir()
        monkeypatch.setenv("BRAIN_HOME", str(custom_vault))
        cfg_path = write_config(
            sources=[
                {
                    "name": "brain",
                    "path": str(custom_vault),  # literal, not "$BRAIN_HOME"
                    "glob": "**/*.md",
                    "frontmatter": "auto-memory",
                    "exclude": [],
                }
            ]
        )
        cfg = load_config()
        # No migration: still single source, no marker stamped
        assert len(cfg.sources) == 1
        on_disk = json.loads(cfg_path.read_text())
        assert _MIGRATION_MARKER_KEY not in on_disk

    def test_user_explicit_env_var_path_skipped(
        self, isolated_xdg, write_config, monkeypatch
    ):
        """User wrote a custom env-var literal (e.g., `$MY_NOTES`) as their
        brain path. Not in the accepted-defaults set → migration must skip.
        Pins the contract: only $BRAIN_ROOT/memory and $BRAIN_HOME are
        recognized as legacy default literals.
        """
        cfg_path = write_config(
            sources=[
                {
                    "name": "brain",
                    "path": "$MY_NOTES",
                    "glob": "**/*.md",
                    "frontmatter": "auto-memory",
                    "exclude": [],
                }
            ]
        )
        cfg = load_config()
        assert len(cfg.sources) == 1
        on_disk = json.loads(cfg_path.read_text())
        assert _MIGRATION_MARKER_KEY not in on_disk

    def test_user_tilde_path_skipped(
        self, isolated_xdg, write_config, monkeypatch
    ):
        """User wrote a `~`-prefixed path as a literal. `~` is not in the
        accepted-defaults set → migration must skip. Defensive pin against
        a future regression that might loosen the path-equality gate.
        """
        cfg_path = write_config(
            sources=[
                {
                    "name": "brain",
                    "path": "~/some/notes",
                    "glob": "**/*.md",
                    "frontmatter": "auto-memory",
                    "exclude": [],
                }
            ]
        )
        cfg = load_config()
        assert len(cfg.sources) == 1
        on_disk = json.loads(cfg_path.read_text())
        assert _MIGRATION_MARKER_KEY not in on_disk

    def test_user_with_multiple_sources_skips_migration(
        self, isolated_xdg, write_config, monkeypatch, tmp_path
    ):
        """User has already added their own second source (e.g. an
        Obsidian vault) → don't touch their config. The single-source
        precondition is what tells us 'this is a pre-feature config.'"""
        monkeypatch.setenv("BRAIN_ROOT", str(isolated_xdg / "agent"))
        write_config(
            sources=[
                {
                    "name": "brain",
                    "path": "$BRAIN_ROOT/memory",
                    "glob": "**/*.md",
                    "frontmatter": "auto-memory",
                    "exclude": [],
                },
                {
                    "name": "vault",
                    "path": str(tmp_path / "vault"),
                    "glob": "**/*.md",
                    "frontmatter": "optional",
                    "exclude": [],
                },
            ]
        )
        cfg = load_config()
        # Source list unchanged — vault preserved, no `imports` added
        assert {s.name for s in cfg.sources} == {"brain", "vault"}

    def test_user_marker_set_with_one_source_is_respected(
        self, isolated_xdg, write_config, monkeypatch
    ):
        """User deliberately removed `imports` and stamped the marker
        themselves (or had migration run earlier and then removed
        imports). Don't re-add it on next load."""
        monkeypatch.setenv("BRAIN_ROOT", str(isolated_xdg / "agent"))
        write_config(
            sources=[
                {
                    "name": "brain",
                    "path": "$BRAIN_ROOT/memory",
                    "glob": "**/*.md",
                    "frontmatter": "auto-memory",
                    "exclude": [],
                }
            ],
            extra={_MIGRATION_MARKER_KEY: _MIGRATION_MARKER_V2},
        )
        cfg = load_config()
        assert len(cfg.sources) == 1
        assert cfg.sources[0].name == "brain"

    def test_fresh_default_config_stamps_marker(
        self, isolated_xdg, monkeypatch
    ):
        """A fresh install (no config file) produces the new defaults
        AND saves the marker — so a downgrade-and-re-upgrade cycle
        won't accidentally re-trigger migration on already-migrated
        content. (load_config writes the default to disk on missing.)"""
        monkeypatch.setenv("BRAIN_ROOT", str(isolated_xdg / "agent"))
        cfg = load_config()
        assert [s.name for s in cfg.sources] == ["brain", "imports"]
        on_disk = json.loads(config_path().read_text())
        assert on_disk.get(_MIGRATION_MARKER_KEY) == _MIGRATION_MARKER_V2

        # And re-load is a no-op: marker is already stamped, so a fresh
        # load doesn't rewrite. Closes the loop with `test_migration_is_idempotent`
        # — that one covers post-migration; this covers post-fresh-default.
        first_text = config_path().read_text()
        load_config()
        assert config_path().read_text() == first_text


class TestLoadConfig:
    def test_creates_default_when_missing(self, isolated_xdg):
        cfg = load_config()
        assert cfg.sources[0].name == "brain"
        # Should have written the file
        assert config_path().exists()

    def test_reads_existing(self, isolated_xdg, write_config):
        write_config(
            sources=[
                {
                    "name": "test",
                    "path": "/tmp/test",
                    "glob": "**/*.md",
                    "frontmatter": "optional",
                    "exclude": [],
                }
            ]
        )
        cfg = load_config()
        assert len(cfg.sources) == 1
        assert cfg.sources[0].name == "test"
        assert cfg.sources[0].frontmatter == "optional"

    def test_two_sources(self, isolated_xdg, write_config, tmp_path):
        write_config(
            sources=[
                {
                    "name": "brain",
                    "path": str(tmp_path / "brain"),
                    "glob": "**/*.md",
                    "frontmatter": "auto-memory",
                    "exclude": ["episodic/**"],
                },
                {
                    "name": "obsidian",
                    "path": str(tmp_path / "vault"),
                    "glob": "**/*.md",
                    "frontmatter": "optional",
                    "exclude": [".obsidian/**"],
                },
            ]
        )
        cfg = load_config()
        assert len(cfg.sources) == 2
        assert {s.name for s in cfg.sources} == {"brain", "obsidian"}

    def test_invalid_json_raises_clear_error(self, isolated_xdg):
        cfg_path = config_path()
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text("not valid json {", encoding="utf-8")
        with pytest.raises((json.JSONDecodeError, ValueError)):
            load_config()

    def test_missing_required_field_raises(self, isolated_xdg, write_config):
        write_config(sources=[{"name": "missing-path"}])  # no path/glob/frontmatter
        with pytest.raises((KeyError, ValueError, TypeError)):
            load_config()

    def test_duplicate_source_names_rejected(self, isolated_xdg, write_config, tmp_path):
        write_config(
            sources=[
                {
                    "name": "dup",
                    "path": str(tmp_path / "a"),
                    "glob": "**/*.md",
                    "frontmatter": "optional",
                    "exclude": [],
                },
                {
                    "name": "dup",
                    "path": str(tmp_path / "b"),
                    "glob": "**/*.md",
                    "frontmatter": "optional",
                    "exclude": [],
                },
            ]
        )
        with pytest.raises(ValueError):
            load_config()


class TestSourceConfig:
    def test_path_is_resolved_absolute(self, tmp_path):
        sc = SourceConfig(
            name="test",
            path=str(tmp_path),
            glob="**/*.md",
            frontmatter="optional",
            exclude=[],
        )
        # After resolution, should be absolute
        assert Path(sc.path).is_absolute()

    def test_invalid_frontmatter_value_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            SourceConfig(
                name="test",
                path=str(tmp_path),
                glob="**/*.md",
                frontmatter="bogus-mode",  # only auto-memory and optional valid
                exclude=[],
            )
