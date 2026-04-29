"""Tests for config loading and BRAIN_HOME / XDG resolution."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from recall.config import (
    Config,
    SourceConfig,
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
    def test_returns_brain_source(self, isolated_xdg):
        cfg = default_config()
        assert len(cfg.sources) == 1
        assert cfg.sources[0].name == "brain"
        assert cfg.sources[0].frontmatter == "auto-memory"

    def test_excludes_episodic_and_working(self, isolated_xdg):
        cfg = default_config()
        excludes = cfg.sources[0].exclude
        assert any("episodic" in e for e in excludes)
        assert any("working" in e for e in excludes)
        assert any("scripts" in e for e in excludes)

    def test_default_k_is_5(self, isolated_xdg):
        cfg = default_config()
        assert cfg.default_k == 5


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
