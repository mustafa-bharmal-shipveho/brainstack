"""Direct CLI tests using Typer's CliRunner.

Subprocess-based tests in `test_cli.py` confirm end-to-end behavior but don't
register coverage on the CLI module itself. These run the Typer app in-process
to cover the dispatch logic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from recall.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_help(runner):
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "query" in result.stdout.lower()


def test_sources_default(runner, isolated_xdg):
    result = runner.invoke(app, ["sources"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert isinstance(data, list)


def test_doctor_default(runner, isolated_xdg):
    result = runner.invoke(app, ["doctor"])
    # Doctor prints to stdout; exit code 0 if no issues
    assert "recall doctor" in result.stdout
    assert "BRAIN_HOME" in result.stdout


def test_reindex_empty_brain(runner, isolated_xdg, write_config, empty_brain):
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
    result = runner.invoke(app, ["reindex"])
    assert result.exit_code == 0


def test_query_returns_results(runner, isolated_xdg, write_config, auto_memory_brain):
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
    runner.invoke(app, ["reindex"])
    result = runner.invoke(app, ["query", "atomic", "writes"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert len(data) >= 1
    # 'atomic-writes' should be near the top
    top_names = [d.get("name") for d in data[:3]]
    assert "atomic-writes" in top_names


def test_query_with_k_flag(runner, isolated_xdg, write_config, auto_memory_brain):
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
    runner.invoke(app, ["reindex"])
    result = runner.invoke(app, ["query", "--k", "2", "memory"])
    data = json.loads(result.stdout)
    assert len(data) <= 2


def test_query_type_filter(runner, isolated_xdg, write_config, auto_memory_brain):
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
    runner.invoke(app, ["reindex"])
    result = runner.invoke(app, ["query", "--type", "feedback", "memory"])
    data = json.loads(result.stdout)
    assert all(d.get("type") == "feedback" for d in data)


def test_query_source_filter(
    runner, isolated_xdg, write_config, auto_memory_brain, generic_brain
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
    runner.invoke(app, ["reindex"])
    result = runner.invoke(app, ["query", "--source", "vault", "lasagna"])
    data = json.loads(result.stdout)
    assert all(d.get("source") == "vault" for d in data)


def test_doctor_reports_missing_path(runner, isolated_xdg, write_config):
    write_config(
        sources=[
            {
                "name": "ghost",
                "path": "/nonexistent/path/should/not/exist",
                "glob": "**/*.md",
                "frontmatter": "optional",
                "exclude": [],
            }
        ]
    )
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "ghost" in result.stdout or "/nonexistent" in result.stdout
