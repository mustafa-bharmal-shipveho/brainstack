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

def test_timeline_no_log_friendly_message(cli_env: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["timeline"])
    assert result.exit_code == 0
    assert "no events" in result.output.lower()


def test_timeline_default_is_flight_recorder_summary(cli_env: Path) -> None:
    """Default `recall runtime timeline` is a flight-recorder digest, not a firehose."""
    _populate_log(cli_env)
    runner = CliRunner()
    result = runner.invoke(app, ["timeline"])
    assert result.exit_code == 0
    # Flight-recorder narrative keywords
    assert "Flight recorder" in result.output
    assert "Claude saw" in result.output
    assert "still in memory" in result.output
    assert "Memory now:" in result.output
    # Should NOT contain per-event lines like "+ Read"
    assert "+ Read" not in result.output


def test_timeline_full_shows_every_event(cli_env: Path) -> None:
    """`recall runtime timeline --full` is the chronological firehose."""
    _populate_log(cli_env)
    runner = CliRunner()
    result = runner.invoke(app, ["timeline", "--full"])
    assert result.exit_code == 0
    assert "SessionStart" in result.output
    assert "UserPromptSubmit" in result.output
    # Per-event lines reappear in --full mode
    assert "+ Read" in result.output or "200 tok" in result.output


def test_timeline_full_shows_tool_invocations_with_token_counts(cli_env: Path) -> None:
    _populate_log(cli_env)
    runner = CliRunner()
    result = runner.invoke(app, ["timeline", "--full"])
    assert result.exit_code == 0
    assert "200 tok" in result.output
    assert "retrieved" in result.output


def test_timeline_summary_mentions_evictions(cli_env: Path) -> None:
    """When the budget is breached, the timeline must mark the offending
    line with 'EVICTS [...]' so the cause-and-effect is visible."""
    from runtime.core.events import EVENT_LOG_SCHEMA_VERSION, EventRecord, append_event
    from runtime.core.manifest import InjectionItemSnapshot

    log = cli_env / "logs" / "events.log.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    snap = lambda i, t: InjectionItemSnapshot(
        id=f"c-{i}", bucket="retrieved", source_path=f"p/{i}.md",
        sha256="0" * 64, token_count=t, retrieval_reason="r",
        last_touched_turn=0, pinned=False, score=0.0,
    )
    # Tight budget so the third add forces eviction. The cli's _replay_config
    # uses cfg.budgets which we set to retrieved=10000 in the cli_env fixture.
    # Override by tightening the fixture inline:
    pyproject = cli_env / "pyproject.toml"
    pyproject.write_text(
        '[tool.recall.runtime]\n'
        f'log_dir = "{cli_env}/logs"\n'
        '[tool.recall.runtime.budget]\n'
        'retrieved = 800\n'
    )
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=1, event="SessionStart", session_id="s", turn=0))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=2, event="UserPromptSubmit", session_id="s", turn=1))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=3, event="PostToolUse", session_id="s", turn=1, tool_name="Read", items_added=[snap(0, 400)]))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=4, event="PostToolUse", session_id="s", turn=1, tool_name="Read", items_added=[snap(1, 400)]))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=5, event="PostToolUse", session_id="s", turn=1, tool_name="Grep", items_added=[snap(2, 700)]))

    runner = CliRunner()
    # Default summary mode: flight-recorder narrative mentions drops + breaches
    result = runner.invoke(app, ["timeline"])
    assert result.exit_code == 0
    assert "dropped" in result.output
    assert "budget breach" in result.output
    # --full mode: shows the EVICTS marker inline on the offending event
    result_full = runner.invoke(app, ["timeline", "--full"])
    assert result_full.exit_code == 0
    assert "EVICTS" in result_full.output


def test_timeline_works_on_single_turn_session(cli_env: Path) -> None:
    """The exact case --diff failed on. Should print all events without complaint."""
    from runtime.core.events import EVENT_LOG_SCHEMA_VERSION, EventRecord, append_event
    from runtime.core.manifest import InjectionItemSnapshot

    log = cli_env / "logs" / "events.log.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    snap = lambda i: InjectionItemSnapshot(
        id=f"c-{i:03d}", bucket="retrieved", source_path=f"p/{i}.md",
        sha256="0" * 64, token_count=100, retrieval_reason="r",
        last_touched_turn=0, pinned=False, score=0.0,
    )
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=1, event="SessionStart", session_id="s", turn=0))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=2, event="UserPromptSubmit", session_id="s", turn=1))
    for i in range(33):
        append_event(log, EventRecord(
            EVENT_LOG_SCHEMA_VERSION, ts_ms=10 + i, event="PostToolUse",
            session_id="s", turn=1, tool_name="Read", items_added=[snap(i)],
        ))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=100, event="Stop", session_id="s", turn=1))

    runner = CliRunner()
    # Default summary mode: 33 items aggregated into a digest
    result = runner.invoke(app, ["timeline"])
    assert result.exit_code == 0
    assert "33" in result.output  # appears in events count or items added
    # --full mode: every event listed
    result_full = runner.invoke(app, ["timeline", "--full"])
    assert result_full.exit_code == 0
    assert result_full.output.count("+ Read") == 33
    assert "33 items total" in result_full.output
    assert "retrieved=33items" in result_full.output


def test_pin_writes_event_log_entry(cli_env: Path) -> None:
    """Pin/unpin write Pin/Unpin events to the log so replay applies them."""
    runner = CliRunner()
    result = runner.invoke(app, ["pin", "c-001"])
    assert result.exit_code == 0
    log = cli_env / "logs" / "events.log.jsonl"
    assert log.exists()
    text = log.read_text()
    assert '"event":"Pin"' in text
    assert "c-001" in text


def test_unpin_writes_event_log_entry(cli_env: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["unpin", "c-001"])
    assert result.exit_code == 0
    log = cli_env / "logs" / "events.log.jsonl"
    text = log.read_text()
    assert '"event":"Unpin"' in text
    assert "c-001" in text


def test_pin_event_replay_marks_item_pinned(cli_env: Path) -> None:
    """Integration: add an item, pin it via CLI, replay shows it pinned."""
    _populate_log(cli_env)  # adds c-001 (unpinned)
    runner = CliRunner()
    result = runner.invoke(app, ["pin", "c-001"])
    assert result.exit_code == 0
    # Now ls should show c-001 as pinned (the * marker)
    result = runner.invoke(app, ["ls"])
    assert result.exit_code == 0
    # Find the line with c-001 and check for the pinned marker
    lines = [ln for ln in result.output.splitlines() if "c-001" in ln]
    assert lines, "c-001 not found in ls output"
    assert any("*" in ln for ln in lines), f"no pinned marker in: {lines}"


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
