"""Tests for the recall CLI surface (subprocess-based)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


def run_cli(args: list[str], env: dict | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Invoke the CLI as a subprocess and return the completed process."""
    full_env = {}
    import os

    full_env.update(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "recall.cli", *args],
        capture_output=True,
        text=True,
        env=full_env,
        cwd=str(cwd) if cwd else None,
    )


def _xdg_env(isolated_xdg: Path) -> dict:
    return {
        "XDG_CONFIG_HOME": str(isolated_xdg / "xdg-config"),
        "XDG_CACHE_HOME": str(isolated_xdg / "xdg-cache"),
        "XDG_DATA_HOME": str(isolated_xdg / "xdg-data"),
        "BRAIN_HOME": str(isolated_xdg / "xdg-data" / "brain"),
    }


class TestCliBasics:
    def test_no_args_prints_help(self, isolated_xdg):
        result = run_cli([], env=_xdg_env(isolated_xdg))
        # Either exit 0 with help, or non-zero — but stdout/stderr should mention recall
        combined = result.stdout + result.stderr
        assert "recall" in combined.lower() or "usage" in combined.lower()

    def test_help_flag(self, isolated_xdg):
        result = run_cli(["--help"], env=_xdg_env(isolated_xdg))
        assert result.returncode == 0
        assert "query" in result.stdout.lower()


class TestCliSources:
    def test_sources_lists_default(self, isolated_xdg):
        result = run_cli(["sources"], env=_xdg_env(isolated_xdg))
        assert result.returncode == 0
        assert "brain" in result.stdout

    def test_sources_with_custom_config(self, isolated_xdg, write_config, auto_memory_brain):
        write_config(
            sources=[
                {
                    "name": "test-brain",
                    "path": str(auto_memory_brain),
                    "glob": "**/*.md",
                    "frontmatter": "auto-memory",
                    "exclude": [],
                }
            ]
        )
        result = run_cli(["sources"], env=_xdg_env(isolated_xdg))
        assert result.returncode == 0
        assert "test-brain" in result.stdout


class TestCliReindex:
    def test_reindex_succeeds_on_real_brain(self, isolated_xdg, write_config, auto_memory_brain):
        write_config(
            sources=[
                {
                    "name": "brain",
                    "path": str(auto_memory_brain),
                    "glob": "**/*.md",
                    "frontmatter": "auto-memory",
                    "exclude": [],
                }
            ]
        )
        result = run_cli(["reindex"], env=_xdg_env(isolated_xdg))
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_reindex_on_empty_brain_is_okay(self, isolated_xdg, write_config, empty_brain):
        write_config(
            sources=[
                {
                    "name": "empty",
                    "path": str(empty_brain),
                    "glob": "**/*.md",
                    "frontmatter": "optional",
                    "exclude": [],
                }
            ]
        )
        result = run_cli(["reindex"], env=_xdg_env(isolated_xdg))
        assert result.returncode == 0


class TestCliQuery:
    def test_query_returns_json(self, isolated_xdg, write_config, auto_memory_brain):
        write_config(
            sources=[
                {
                    "name": "brain",
                    "path": str(auto_memory_brain),
                    "glob": "**/*.md",
                    "frontmatter": "auto-memory",
                    "exclude": [],
                }
            ]
        )
        run_cli(["reindex"], env=_xdg_env(isolated_xdg))
        result = run_cli(
            ["query", "pin dependencies version lockfile"], env=_xdg_env(isolated_xdg)
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert len(data) >= 1

    def test_query_top_hit_correct(self, isolated_xdg, write_config, auto_memory_brain):
        write_config(
            sources=[
                {
                    "name": "brain",
                    "path": str(auto_memory_brain),
                    "glob": "**/*.md",
                    "frontmatter": "auto-memory",
                    "exclude": [],
                }
            ]
        )
        run_cli(["reindex"], env=_xdg_env(isolated_xdg))
        result = run_cli(
            ["query", "pin dependencies version lockfile"], env=_xdg_env(isolated_xdg)
        )
        data = json.loads(result.stdout)
        assert data[0]["name"] == "pin-dependencies"

    def test_query_with_k_flag(self, isolated_xdg, write_config, auto_memory_brain):
        write_config(
            sources=[
                {
                    "name": "brain",
                    "path": str(auto_memory_brain),
                    "glob": "**/*.md",
                    "frontmatter": "auto-memory",
                    "exclude": [],
                }
            ]
        )
        run_cli(["reindex"], env=_xdg_env(isolated_xdg))
        result = run_cli(["query", "--k", "2", "memory"], env=_xdg_env(isolated_xdg))
        data = json.loads(result.stdout)
        assert len(data) <= 2

    def test_query_with_type_filter(self, isolated_xdg, write_config, auto_memory_brain):
        write_config(
            sources=[
                {
                    "name": "brain",
                    "path": str(auto_memory_brain),
                    "glob": "**/*.md",
                    "frontmatter": "auto-memory",
                    "exclude": [],
                }
            ]
        )
        run_cli(["reindex"], env=_xdg_env(isolated_xdg))
        result = run_cli(
            ["query", "--type", "reference", "memory"], env=_xdg_env(isolated_xdg)
        )
        data = json.loads(result.stdout)
        assert all(d.get("type") == "reference" for d in data)

    def test_query_with_source_filter(
        self, isolated_xdg, write_config, auto_memory_brain, generic_brain
    ):
        write_config(
            sources=[
                {
                    "name": "brain",
                    "path": str(auto_memory_brain),
                    "glob": "**/*.md",
                    "frontmatter": "auto-memory",
                    "exclude": [],
                },
                {
                    "name": "vault",
                    "path": str(generic_brain),
                    "glob": "**/*.md",
                    "frontmatter": "optional",
                    "exclude": [],
                },
            ]
        )
        run_cli(["reindex"], env=_xdg_env(isolated_xdg))
        result = run_cli(
            ["query", "--source", "vault", "lasagna"], env=_xdg_env(isolated_xdg)
        )
        data = json.loads(result.stdout)
        assert all(d.get("source") == "vault" for d in data)


class TestCliDoctor:
    def test_doctor_runs_on_default_config(self, isolated_xdg):
        result = run_cli(["doctor"], env=_xdg_env(isolated_xdg))
        # Doctor should always run; may report warnings but shouldn't crash
        assert result.returncode in (0, 1)
        combined = result.stdout + result.stderr
        # Should mention deps or sources or paths
        assert any(kw in combined.lower() for kw in ["sources", "config", "brain", "depend"])

    def test_doctor_reports_missing_path(self, isolated_xdg, write_config):
        write_config(
            sources=[
                {
                    "name": "ghost",
                    "path": "/nonexistent/path/brain",
                    "glob": "**/*.md",
                    "frontmatter": "optional",
                    "exclude": [],
                }
            ]
        )
        result = run_cli(["doctor"], env=_xdg_env(isolated_xdg))
        # Should report the missing path
        combined = result.stdout + result.stderr
        assert "ghost" in combined or "/nonexistent" in combined
