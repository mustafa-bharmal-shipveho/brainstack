"""CLI surface for the health catalogue: `recall health` and `recall doctor --health`.

The checks themselves are pinned in tests/recall/test_health.py. Here we only
care about the command contract: exit codes, `--json` shape, `--write`, and
where the brain root comes from. `recall.health.CHECKS` is replaced with
constant-valued stubs so no test shells out to git or launchctl.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from recall import health
from recall.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def stub_checks(monkeypatch):
    """Replace the catalogue with checks that return fixed results."""

    def _stub(*results: health.CheckResult) -> tuple[health.CheckResult, ...]:
        checks = tuple(
            (lambda fixed: (lambda env: fixed))(r) for r in results
        )
        monkeypatch.setattr(health, "CHECKS", checks)
        return results

    return _stub


def _cr(id_: str, status: str, evidence: str = "evidence here", fix: str = ""):
    return health.CheckResult(id=id_, status=status, evidence=evidence, fix=fix)


def test_health_exit_1_on_fail(runner, isolated_xdg, stub_checks, tmp_path: Path):
    stub_checks(
        _cr("drift", "PASS", "in sync"),
        _cr("brain_push", "FAIL", "67 commits ahead of origin/main",
            "see large_tracked_files"),
    )

    result = runner.invoke(
        app, ["health", "--brain-root", str(tmp_path), "--cwd", str(tmp_path)]
    )

    assert result.exit_code == 1
    assert "== recall health ==" in result.stdout
    assert "brain_push" in result.stdout
    assert "67 commits ahead of origin/main" in result.stdout
    assert "fix: see large_tracked_files" in result.stdout


def test_health_exit_0_all_pass(runner, isolated_xdg, stub_checks, tmp_path: Path):
    stub_checks(_cr("drift", "PASS", "in sync"), _cr("daemon", "SKIP", "not configured"))

    result = runner.invoke(
        app, ["health", "--brain-root", str(tmp_path), "--cwd", str(tmp_path)]
    )

    assert result.exit_code == 0

    # A WARN is information, not a failure: exit stays 0 so the hourly sync
    # LaunchAgent does not record a spurious non-zero exit every hour.
    stub_checks(_cr("log_sizes", "WARN", "events.log.jsonl 66.4 MB exceed 20 MB"))
    warned = runner.invoke(
        app, ["health", "--brain-root", str(tmp_path), "--cwd", str(tmp_path)]
    )
    assert warned.exit_code == 0


def test_health_json_shape(runner, isolated_xdg, stub_checks, tmp_path: Path):
    stub_checks(
        _cr("imports_freshness", "FAIL", "never mirrored",
            "./install.sh --setup-claude-extras"),
        _cr("daemon", "SKIP", "not configured"),
    )

    result = runner.invoke(
        app,
        ["health", "--json", "--brain-root", str(tmp_path), "--cwd", str(tmp_path)],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == health.SCHEMA_VERSION
    assert payload["brain_root"] == str(tmp_path)
    assert payload["cwd"] == str(tmp_path)
    assert payload["status"] == "FAIL"
    assert payload["counts"]["FAIL"] == 1
    assert payload["counts"]["SKIP"] == 1
    assert payload["generated_at"].endswith("Z")
    assert payload["checks"][0] == {
        "id": "imports_freshness",
        "status": "FAIL",
        "evidence": "never mirrored",
        "fix": "./install.sh --setup-claude-extras",
    }


def test_health_write_creates_file(runner, isolated_xdg, stub_checks, tmp_path: Path):
    stub_checks(_cr("log_sizes", "WARN", "events.log.jsonl 66.4 MB exceed 20 MB"))
    out = tmp_path / "brain" / "runtime" / "health.json"

    result = runner.invoke(app, [
        "health",
        "--brain-root", str(tmp_path),
        "--cwd", str(tmp_path),
        "--write", str(out),
    ])

    assert result.exit_code == 0
    assert out.is_file()
    payload = json.loads(out.read_text())
    assert payload["status"] == "WARN"
    assert payload["checks"][0]["id"] == "log_sizes"


def test_health_brain_root_option_wins_over_env(
    runner, isolated_xdg, monkeypatch, tmp_path: Path
):
    """`--brain-root` beats `$BRAIN_ROOT`; without the flag the env wins.

    `build_env` is NOT stubbed here — resolving the brain root is the thing
    under test. Only the catalogue is replaced, with a probe that echoes the
    root the environment was built with.
    """
    env_brain = tmp_path / "env-brain"
    opt_brain = tmp_path / "opt-brain"
    env_brain.mkdir()
    opt_brain.mkdir()
    monkeypatch.setenv("BRAIN_ROOT", str(env_brain))
    monkeypatch.setattr(health, "CHECKS", (
        lambda env: health.CheckResult("probe", "PASS", str(env.brain_root)),
    ))

    from_env = runner.invoke(app, ["health", "--cwd", str(tmp_path)])
    assert from_env.exit_code == 0
    assert str(env_brain) in from_env.stdout

    from_opt = runner.invoke(
        app, ["health", "--brain-root", str(opt_brain), "--cwd", str(tmp_path)]
    )
    assert from_opt.exit_code == 0
    assert str(opt_brain) in from_opt.stdout
    assert str(env_brain) not in from_opt.stdout


def test_doctor_health_flag_appends_section_and_exit_code(
    runner, isolated_xdg, stub_checks, monkeypatch, tmp_path: Path
):
    """`recall doctor --health` is the alias for users who only remember
    `doctor`. It keeps doctor's own output and appends the health report;
    a health FAIL alone is enough to exit 1."""
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path))
    stub_checks(
        _cr("brain_push", "FAIL", "67 commits ahead of origin/main",
            "see large_tracked_files"),
    )

    result = runner.invoke(app, ["doctor", "--health"])

    assert "== recall doctor ==" in result.stdout
    assert "== recall health ==" in result.stdout
    assert "67 commits ahead of origin/main" in result.stdout
    assert result.exit_code == 1


@pytest.mark.machine
def test_recall_health_runs_against_live_brain(runner):
    """Dev smoke: the real catalogue against the real machine. Excluded from
    CI (`-m "not machine"`) because it reads the developer's HOME."""
    result = runner.invoke(app, ["health"])

    assert result.exit_code in (0, 1)
    assert "== recall health ==" in result.stdout
    assert "overall:" in result.stdout
