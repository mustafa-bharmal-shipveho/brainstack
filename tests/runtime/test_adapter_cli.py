"""Phase 4b: CLI subcommand tests.

Uses typer.testing.CliRunner to drive `recall runtime` subcommands against
synthetic event logs in tmp paths. No real Claude session needed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from runtime.adapters.claude_code.cli import app
from runtime.adapters.claude_code.installer import install_claude_code_hooks
from runtime.core.events import EVENT_LOG_SCHEMA_VERSION, EventRecord, append_event
from runtime.core.manifest import InjectionItemSnapshot


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch):
    """Point the runtime config at a tmp dir."""
    cfg = tmp_path / "pyproject.toml"
    cfg.write_text(
        '[tool.recall.runtime]\n'
        f'log_dir = "{tmp_path}/logs"\n'
        '[tool.recall.runtime.budget]\n'
        'retrieved = 10000\n'
    )
    monkeypatch.setenv("RECALL_RUNTIME_CONFIG", str(cfg))
    return tmp_path


def _populate_log(log_dir: Path) -> None:
    log = log_dir / "logs" / "events.log.jsonl"
    snap = InjectionItemSnapshot(
        id="c-001", bucket="retrieved", source_path="hot/lessons/foo.md",
        sha256="0" * 64, token_count=200, retrieval_reason="test",
        last_touched_turn=0, pinned=False, score=0.0,
    )
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=1, event="SessionStart", session_id="s", turn=0))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=2, event="UserPromptSubmit", session_id="s", turn=1))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=3, event="PostToolUse", session_id="s", turn=1, items_added=[snap]))


# ---------- ls ----------

def test_ls_no_log_emits_friendly_message(cli_env: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["ls"])
    assert result.exit_code == 0
    assert "no events" in result.output.lower()


def test_ls_after_event_shows_items(cli_env: Path) -> None:
    _populate_log(cli_env)
    runner = CliRunner()
    result = runner.invoke(app, ["ls"])
    assert result.exit_code == 0
    assert "c-001" in result.output
    assert "retrieved" in result.output


def test_ls_json_output(cli_env: Path) -> None:
    _populate_log(cli_env)
    runner = CliRunner()
    result = runner.invoke(app, ["ls", "--json"])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["schema_version"] == "1.1"


# ---------- budget ----------

def test_budget_command_shows_caps(cli_env: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["budget"])
    assert result.exit_code == 0
    assert "retrieved" in result.output
    assert "10000" in result.output


# ---------- replay ----------

def test_replay_with_no_log_exits_nonzero(cli_env: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["replay"])
    assert result.exit_code != 0


def test_replay_basic(cli_env: Path) -> None:
    _populate_log(cli_env)
    runner = CliRunner()
    result = runner.invoke(app, ["replay"])
    assert result.exit_code == 0
    assert "replayed" in result.output


def test_replay_diff_invalid_format(cli_env: Path) -> None:
    _populate_log(cli_env)
    runner = CliRunner()
    result = runner.invoke(app, ["replay", "--diff", "not-a-pair"])
    assert result.exit_code != 0


# ---------- pin / unpin ----------

def test_pin_then_unpin_roundtrip(cli_env: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["pin", "c-001"])
    assert result.exit_code == 0
    pin_file = cli_env / "logs" / "pinned.json"
    assert pin_file.exists()
    assert "c-001" in json.loads(pin_file.read_text())

    result = runner.invoke(app, ["unpin", "c-001"])
    assert result.exit_code == 0
    assert "c-001" not in json.loads(pin_file.read_text())


def test_pin_idempotent(cli_env: Path) -> None:
    runner = CliRunner()
    runner.invoke(app, ["pin", "c-001"])
    runner.invoke(app, ["pin", "c-001"])
    runner.invoke(app, ["pin", "c-001"])
    pin_file = cli_env / "logs" / "pinned.json"
    parsed = json.loads(pin_file.read_text())
    assert parsed.count("c-001") == 1


# ---------- evict ----------

def test_evict_appends_event(cli_env: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["evict", "c-001"])
    assert result.exit_code == 0
    log = cli_env / "logs" / "events.log.jsonl"
    assert log.exists()
    text = log.read_text()
    assert "c-001" in text
    assert "item_ids_evicted" in text


# ---------- install-hooks ----------

def test_installer_idempotent(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    r1 = install_claude_code_hooks(settings_path=settings)
    r2 = install_claude_code_hooks(settings_path=settings)
    assert r1.added == ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"]
    assert r2.added == []
    assert sorted(r2.already_present) == sorted(r1.added)


def test_installer_preserves_existing_hooks(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "echo existing"}]}],
        },
    }))
    install_claude_code_hooks(settings_path=settings)
    parsed = json.loads(settings.read_text())
    stop_entries = parsed["hooks"]["Stop"]
    # Existing entry preserved + brainstack entry added
    cmds = []
    for e in stop_entries:
        for h in e.get("hooks", []) or []:
            cmds.append(h.get("command", ""))
    assert any("echo existing" in c for c in cmds)
    assert any("brainstack-runtime" in c for c in cmds)


def test_installer_dry_run_does_not_modify(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    install_claude_code_hooks(settings_path=settings, dry_run=True)
    assert settings.read_text() == "{}"


def test_installer_handles_missing_settings(tmp_path: Path) -> None:
    settings = tmp_path / "fresh-install" / "settings.json"
    install_claude_code_hooks(settings_path=settings)
    assert settings.exists()
    parsed = json.loads(settings.read_text())
    assert "hooks" in parsed


def test_installer_refuses_invalid_json(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("{not valid json")
    report = install_claude_code_hooks(settings_path=settings)
    assert report.error
    assert settings.read_text() == "{not valid json"  # untouched
