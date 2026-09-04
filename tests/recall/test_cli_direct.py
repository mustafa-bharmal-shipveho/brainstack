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


# ---------------------------------------------------------------------------
# Warm daemon routing (S3)
#
# While `recall serve` runs it OWNS the embedded Qdrant store (exclusive
# process lock). Anything that opens the store directly gets "index is busy",
# so the CLI has to route through the socket instead of racing it. These
# tests pin the three user-visible surfaces of that: doctor's health line,
# `serve --status` as a scriptable probe, and `query --no-daemon` as the
# escape hatch.
# ---------------------------------------------------------------------------


def _short_sock_dir():
    """AF_UNIX paths are capped at 104 bytes on macOS; tmp_path exceeds it."""
    import shutil
    import tempfile
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        d = tempfile.mkdtemp(prefix="rsd-", dir="/tmp")
        try:
            yield Path(d)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    return _ctx()


def test_doctor_reports_daemon_not_running(runner, isolated_xdg, monkeypatch, tmp_path):
    """Doctor states the daemon's status. A missing daemon is not an error —
    it means every hook pays the slow in-process path, which the user needs
    told, with the command that fixes it."""
    _isolate_home(monkeypatch, tmp_path)
    _setup_brain_dirs()
    with _short_sock_dir() as sock_dir:
        monkeypatch.setenv("RECALL_DAEMON_SOCKET", str(sock_dir / "absent.sock"))

        result = runner.invoke(app, ["doctor"])

    assert "Daemon: not running" in result.output, (
        f"doctor must report daemon status:\n{result.output}"
    )
    assert "--setup-daemon" in result.output, (
        f"the not-running note must point at the fix:\n{result.output}"
    )


def test_serve_status_exit_1_when_absent(runner, isolated_xdg, monkeypatch):
    """`recall serve --status` is a scriptable probe: exit 1 = not running.

    launchd health checks and `install.sh` both branch on the exit code, so
    a crash or a 0 here would report a dead daemon as healthy.
    """
    with _short_sock_dir() as sock_dir:
        monkeypatch.setenv("RECALL_DAEMON_SOCKET", str(sock_dir / "absent.sock"))

        result = runner.invoke(app, ["serve", "--status"])

    assert result.exit_code == 1, (
        f"--status must exit 1 when no daemon is running:\n{result.output}"
    )
    assert "not running" in result.output.lower(), result.output


def test_serve_status_works_without_the_daemon_module(
    runner, isolated_xdg, monkeypatch
):
    """`--status` and `doctor`'s daemon note resolve the socket through
    `recall.config`, never `recall.daemon`.

    Importing the daemon module pulls `recall.index` -> `qdrant_client`, so
    a probe whose whole job is "is anything listening on this path?" either
    pays ~0.9 s for a path join or, on an install where qdrant is not
    importable, fails outright instead of answering "not running".
    `sys.modules[name] = None` is exactly how Python reports an
    unimportable module, so this simulates that install.
    """
    import sys

    monkeypatch.setitem(sys.modules, "recall.daemon", None)
    with _short_sock_dir() as sock_dir:
        monkeypatch.setenv("RECALL_DAEMON_SOCKET", str(sock_dir / "absent.sock"))

        status = runner.invoke(app, ["serve", "--status"])
        notes: list[str] = []
        from recall.cli import _check_daemon, _resolve_daemon_socket

        resolved = _resolve_daemon_socket()
        _check_daemon(notes)

    assert status.exit_code == 1, (
        f"--status must still answer when recall.daemon cannot be imported:\n"
        f"{status.output}\n{status.exception!r}"
    )
    assert "not running" in status.output.lower(), status.output
    assert resolved is not None and resolved.name == "absent.sock", resolved
    assert notes and "Daemon: not running" in notes[0], notes


def test_daemon_probes_do_not_import_the_daemon_module(isolated_xdg, tmp_path):
    """The laziness itself, checked in a clean interpreter.

    An in-process assertion would be meaningless: any earlier test in the
    session may already have imported `recall.daemon`.
    """
    import os
    import subprocess
    import sys
    import textwrap

    probe = textwrap.dedent(
        """
        import sys
        from recall.cli import _check_daemon, _resolve_daemon_socket

        _resolve_daemon_socket()
        _check_daemon([])
        assert "recall.daemon" not in sys.modules, (
            "resolving the daemon socket imported recall.daemon (and with it "
            "recall.index -> qdrant_client) just to join a few path components"
        )
        print("ok")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=180,
        # Pinned at a path nothing listens on: the probe must never reach a
        # daemon the developer actually has running.
        env={**os.environ, "RECALL_DAEMON_SOCKET": str(tmp_path / "absent.sock")},
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )


def test_query_no_daemon_flag_accepted(
    runner, isolated_xdg, write_config, empty_brain
):
    """`--no-daemon` forces the in-process path (debugging, or a wedged
    daemon). It has to parse and behave like today's query."""
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

    result = runner.invoke(app, ["query", "--no-daemon", "anything"])

    assert "No such option" not in result.output, result.output
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "[]"


@pytest.mark.embeddings
def test_query_routes_via_daemon_and_matches_direct_shape(
    runner, isolated_xdg, write_config, auto_memory_brain, monkeypatch
):
    """Daemon-routed results are indistinguishable from direct ones.

    Every consumer of `recall query` parses this JSON. If routing through the
    socket drops or renames a key, the daemon becomes a silent breaking change
    the moment a user installs it.
    """
    import json as _json
    import stat as _stat
    import threading as _threading
    import time as _time
    from dataclasses import dataclass
    from typing import Optional as _Optional

    from recall.daemon import RecallDaemon

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
    assert runner.invoke(app, ["reindex"]).exit_code == 0

    # Shape from the direct path, with the daemon explicitly bypassed.
    direct = runner.invoke(app, ["query", "--no-daemon", "atomic", "writes"])
    assert direct.exit_code == 0, direct.output
    direct_rows = _json.loads(direct.stdout)
    assert direct_rows, "direct query returned nothing to compare against"

    @dataclass
    class _Doc:
        path: str
        source: str
        title: str
        frontmatter: dict
        body: str
        text: str

    @dataclass
    class _Res:
        document: _Doc
        score: float
        rerank_score: _Optional[float] = None

    class _Retriever:
        def query(self, query, k, type_filter=None, source_filter=None, rerank=None):
            return [
                _Res(
                    document=_Doc(
                        path="/brain/semantic/lessons/feedback_atomic_writes.md",
                        source="brain",
                        title="atomic-writes",
                        frontmatter={
                            "name": "atomic-writes",
                            "type": "feedback",
                            "description": "write to tmp then rename",
                        },
                        body="Always write to a tmp file then os.replace it.",
                        text="atomic-writes",
                    ),
                    score=0.42,
                    rerank_score=0.91,
                )
            ][:k]

    with _short_sock_dir() as sock_dir:
        sock = sock_dir / "recall.sock"
        daemon = RecallDaemon(
            socket_path=sock, retriever=_Retriever(), refresh_interval_s=0.0
        )
        t = _threading.Thread(target=daemon.serve_forever, daemon=True)
        t.start()
        deadline = _time.monotonic() + 10
        while _time.monotonic() < deadline and not sock.exists():
            _time.sleep(0.01)
        assert sock.exists() and _stat.S_ISSOCK(sock.stat().st_mode), (
            "fake daemon never bound its socket"
        )
        monkeypatch.setenv("RECALL_DAEMON_SOCKET", str(sock))
        try:
            routed = runner.invoke(app, ["query", "atomic", "writes"])
        finally:
            daemon.shutdown()

    assert routed.exit_code == 0, routed.output
    routed_rows = _json.loads(routed.stdout)
    assert routed_rows, f"daemon-routed query returned nothing:\n{routed.output}"
    assert set(routed_rows[0]) == set(direct_rows[0]), (
        f"daemon-routed JSON shape differs from the direct path.\n"
        f"  daemon only: {set(routed_rows[0]) - set(direct_rows[0])}\n"
        f"  direct only: {set(direct_rows[0]) - set(routed_rows[0])}"
    )
    assert "rerank_score" in routed_rows[0]
    assert routed_rows[0]["rerank_score"] == pytest.approx(0.91)
    # Proof it really came from the daemon, not a silent local fallback.
    assert routed_rows[0]["name"] == "atomic-writes"
    assert routed_rows[0]["score"] == pytest.approx(0.42)
