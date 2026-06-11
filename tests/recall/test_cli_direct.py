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


@pytest.mark.embeddings
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


@pytest.mark.embeddings
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


@pytest.mark.embeddings
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


@pytest.mark.embeddings
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


# ---------------------------------------------------------------------------
# Doctor adoption-audit checks (red phase: behavior not implemented yet)
#
# Planned contract for `recall doctor`:
#   (a) hook-interpreter check: parse ~/.claude/settings.json for commands
#       containing '# brainstack-runtime', extract the interpreter, run
#       [interp, '-c', 'import qdrant_client']. On failure, report an Issue
#       mentioning 'auto-recall' and './install.sh --enable-auto-recall'.
#   (b) print the resolved fastembed cache dir + whether models are cached.
#   (c) print the effective retrieval mode ('hybrid', or a BM25-only
#       fallback note mentioning 'recall reindex').
#   (d) Issue when the brain has a git origin remote but neither trufflehog
#       nor gitleaks is on PATH, mentioning '--install-scanner'.
#   (e) print an 'Install root:' line; Issue when settings.json hook
#       commands reference paths that no longer exist (clone moved).
# ---------------------------------------------------------------------------


def _isolate_home(monkeypatch, tmp_path: Path) -> Path:
    """Point HOME at a tmp dir so doctor never reads the real ~/.claude."""
    home = tmp_path / "doctor-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    return home


def _setup_brain_dirs() -> Path:
    """Create the dirs the auto-generated default config points at, so the
    pre-existing source-missing Issue does not pollute these assertions.

    Under isolated_xdg, BRAIN_HOME is set, so the default config sources
    resolve to $BRAIN_HOME (brain) and its sibling imports/ dir.
    """
    import os

    brain = Path(os.environ["BRAIN_HOME"])
    brain.mkdir(parents=True, exist_ok=True)
    (brain.parent / "imports").mkdir(parents=True, exist_ok=True)
    return brain


def _write_claude_settings(home: Path, command: str) -> Path:
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {"hooks": [{"type": "command", "command": command}]}
                    ]
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return settings


def _brainstack_hook_command(interpreter: str) -> str:
    """Mirror installer._hook_cmd's shape with a chosen interpreter, using
    the REAL pkg root and hooks script so path-existence checks pass."""
    import runtime.adapters.claude_code.installer as installer_mod

    return (
        f"PYTHONPATH={installer_mod._PKG_ROOT} {interpreter} "
        f"{installer_mod._HOOKS_SCRIPT} UserPromptSubmit  # brainstack-runtime"
    )


def test_doctor_flags_hook_interpreter_without_qdrant(
    runner, isolated_xdg, monkeypatch, tmp_path
):
    """A hook pinned to an interpreter lacking qdrant_client = silent
    auto-recall failure on every prompt. Doctor must surface it."""
    home = _isolate_home(monkeypatch, tmp_path)
    _setup_brain_dirs()

    # A stub 'python' whose import probe always fails.
    stub = tmp_path / "stub-python"
    stub.write_text("#!/bin/sh\nexit 1\n")
    stub.chmod(0o755)
    _write_claude_settings(home, _brainstack_hook_command(str(stub)))

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code != 0, (
        f"doctor must fail when the hook interpreter cannot import "
        f"qdrant_client:\n{result.output}"
    )
    assert "auto-recall" in result.output
    assert "./install.sh --enable-auto-recall" in result.output


def test_doctor_passes_hook_interpreter_with_qdrant(
    runner, isolated_xdg, monkeypatch, tmp_path
):
    """Negative control: hook pinned to this test venv's python (which has
    qdrant_client) must NOT raise the auto-recall interpreter Issue."""
    import sys

    home = _isolate_home(monkeypatch, tmp_path)
    _setup_brain_dirs()
    _write_claude_settings(home, _brainstack_hook_command(sys.executable))

    result = runner.invoke(app, ["doctor"])
    assert "--enable-auto-recall" not in result.output, (
        f"healthy hook interpreter must not be flagged:\n{result.output}"
    )
    assert result.exit_code == 0, result.output


def test_doctor_reports_cache_dir_and_model_state(
    runner, isolated_xdg, monkeypatch, tmp_path
):
    """Doctor prints the RESOLVED fastembed cache dir (not a hardcoded
    ~/.cache/fastembed literal) plus whether models are cached."""
    import os

    _isolate_home(monkeypatch, tmp_path)
    _setup_brain_dirs()
    monkeypatch.delenv("FASTEMBED_CACHE_PATH", raising=False)

    result = runner.invoke(app, ["doctor"])
    expected_dir = str(Path(os.environ["XDG_CACHE_HOME"]) / "fastembed")
    assert expected_dir in result.output, (
        f"doctor must print the resolved fastembed cache dir {expected_dir}:\n"
        f"{result.output}"
    )
    assert "cached" in result.output.lower(), (
        f"doctor must say whether models are cached (yes/no):\n{result.output}"
    )


def test_doctor_reports_retrieval_mode_line(
    runner, isolated_xdg, monkeypatch, tmp_path
):
    """Doctor reports the effective retrieval mode: 'hybrid' when the dense
    model is usable, else a BM25-only note pointing at `recall reindex`."""
    _isolate_home(monkeypatch, tmp_path)
    _setup_brain_dirs()

    result = runner.invoke(app, ["doctor"])
    lower = result.output.lower()
    assert "hybrid" in lower or "bm25" in lower, (
        f"doctor must print a retrieval mode line:\n{result.output}"
    )
    if "bm25" in lower:
        assert "recall reindex" in result.output, (
            "the BM25-only fallback note must tell the user to run "
            "'recall reindex'"
        )


def test_doctor_scanner_issue_when_remote_and_no_scanner(
    runner, isolated_xdg, monkeypatch, tmp_path
):
    """Brain pushes to a git remote but no secret scanner is installed:
    sync.sh fails closed, so doctor must point at --install-scanner."""
    import shutil
    import subprocess

    _isolate_home(monkeypatch, tmp_path)
    brain = _setup_brain_dirs()
    subprocess.run(["git", "init", "-q"], cwd=brain, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@example.com:user/brain.git"],
        cwd=brain,
        check=True,
    )

    real_which = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda cmd, *a, **kw: (
            None if cmd in {"trufflehog", "gitleaks"} else real_which(cmd, *a, **kw)
        ),
    )

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code != 0, (
        f"doctor must flag a remote-backed brain with no secret scanner:\n"
        f"{result.output}"
    )
    assert "--install-scanner" in result.output


def test_doctor_notes_install_root(runner, isolated_xdg, monkeypatch, tmp_path):
    _isolate_home(monkeypatch, tmp_path)
    _setup_brain_dirs()

    result = runner.invoke(app, ["doctor"])
    assert "Install root:" in result.output, (
        f"doctor must print an 'Install root:' line:\n{result.output}"
    )


def test_doctor_flags_hooks_pointing_at_missing_clone(
    runner, isolated_xdg, monkeypatch, tmp_path
):
    """Hook commands referencing a path that no longer exists (the clone was
    moved or deleted) must be flagged with a hint that the clone moved."""
    import sys

    home = _isolate_home(monkeypatch, tmp_path)
    _setup_brain_dirs()
    ghost_root = "/nonexistent-brainstack-clone"
    cmd = (
        f"PYTHONPATH={ghost_root} {sys.executable} "
        f"{ghost_root}/runtime/adapters/claude_code/hooks.py UserPromptSubmit"
        f"  # brainstack-runtime"
    )
    _write_claude_settings(home, cmd)

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code != 0, (
        f"doctor must flag hook commands pointing at missing paths:\n"
        f"{result.output}"
    )
    assert "moved" in result.output.lower(), (
        f"the Issue should hint the clone may have moved:\n{result.output}"
    )
