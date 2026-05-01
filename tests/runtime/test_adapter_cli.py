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


def test_timeline_default_scopes_to_most_recent_session(cli_env: Path) -> None:
    """Event log accumulates across sessions. Default `timeline` shows
    only the latest session (events from the last SessionStart to end)."""
    from runtime.core.events import EVENT_LOG_SCHEMA_VERSION, EventRecord, append_event
    from runtime.core.manifest import InjectionItemSnapshot

    log = cli_env / "logs" / "events.log.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    snap = lambda i, sid: InjectionItemSnapshot(
        id=f"c-{sid}-{i:03d}", bucket="retrieved", source_path=f"{sid}/{i}.md",
        sha256="0" * 64, token_count=100, retrieval_reason="r",
        last_touched_turn=0, pinned=False, score=0.0,
    )
    # Older session
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=1, event="SessionStart", session_id="old", turn=0))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=2, event="UserPromptSubmit", session_id="old", turn=1))
    for i in range(20):
        append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=10 + i, event="PostToolUse", session_id="old", turn=1, items_added=[snap(i, "old")]))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=100, event="Stop", session_id="old", turn=1))
    # Newer session
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=200, event="SessionStart", session_id="new", turn=0))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=201, event="UserPromptSubmit", session_id="new", turn=1))
    for i in range(3):
        append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=210 + i, event="PostToolUse", session_id="new", turn=1, items_added=[snap(i, "new")]))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=220, event="Stop", session_id="new", turn=1))

    runner = CliRunner()
    # Default: scope to "new" session (3 PostToolUse, not 23)
    result = runner.invoke(app, ["timeline"])
    assert result.exit_code == 0
    assert "session \"new\"" in result.output
    assert "Claude saw 3 files" in result.output


def test_timeline_all_flag_includes_every_session(cli_env: Path) -> None:
    from runtime.core.events import EVENT_LOG_SCHEMA_VERSION, EventRecord, append_event
    from runtime.core.manifest import InjectionItemSnapshot

    log = cli_env / "logs" / "events.log.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    snap = lambda i, sid: InjectionItemSnapshot(
        id=f"c-{sid}-{i:03d}", bucket="retrieved", source_path=f"{sid}/{i}.md",
        sha256="0" * 64, token_count=100, retrieval_reason="r",
        last_touched_turn=0, pinned=False, score=0.0,
    )
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=1, event="SessionStart", session_id="A", turn=0))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=2, event="PostToolUse", session_id="A", turn=0, items_added=[snap(0, "A")]))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=3, event="SessionStart", session_id="B", turn=0))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=4, event="PostToolUse", session_id="B", turn=0, items_added=[snap(0, "B")]))

    runner = CliRunner()
    # --all should include events from both sessions
    result = runner.invoke(app, ["timeline", "--all"])
    assert result.exit_code == 0
    assert "all sessions" in result.output
    # Should report 2 PostToolUse adds across the two sessions
    assert "Claude saw 2 files" in result.output


def test_timeline_session_flag_picks_specific_session(cli_env: Path) -> None:
    from runtime.core.events import EVENT_LOG_SCHEMA_VERSION, EventRecord, append_event
    from runtime.core.manifest import InjectionItemSnapshot

    log = cli_env / "logs" / "events.log.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    snap = lambda sid: InjectionItemSnapshot(
        id=f"c-{sid}", bucket="retrieved", source_path=f"{sid}.md",
        sha256="0" * 64, token_count=100, retrieval_reason="r",
        last_touched_turn=0, pinned=False, score=0.0,
    )
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=1, event="SessionStart", session_id="alpha", turn=0))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=2, event="PostToolUse", session_id="alpha", turn=0, items_added=[snap("alpha")]))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=3, event="SessionStart", session_id="beta", turn=0))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=4, event="PostToolUse", session_id="beta", turn=0, items_added=[snap("beta")]))

    runner = CliRunner()
    # Pick the older "alpha" session explicitly
    result = runner.invoke(app, ["timeline", "--session", "alpha"])
    assert result.exit_code == 0
    assert "session 'alpha'" in result.output
    assert "Claude saw 1" in result.output


def test_timeline_session_flag_unknown_session_errors(cli_env: Path) -> None:
    _populate_log(cli_env)
    runner = CliRunner()
    result = runner.invoke(app, ["timeline", "--session", "does-not-exist"])
    assert result.exit_code != 0
    assert "no events for session" in result.output


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
    _populate_log(cli_env)  # makes c-001 live in the manifest so evict can resolve it
    runner = CliRunner()
    result = runner.invoke(app, ["evict", "c-001"])
    assert result.exit_code == 0
    log = cli_env / "logs" / "events.log.jsonl"
    assert log.exists()
    text = log.read_text()
    # Both populate-log entries and the new evict entry both contain c-001
    assert "c-001" in text
    assert "item_ids_evicted" in text


def test_evict_with_intent_flag_marks_event(cli_env: Path) -> None:
    """--intent stamps the event with intent='user-evict' for re-injection."""
    _populate_log(cli_env)  # makes c-001 live in the manifest
    runner = CliRunner()
    result = runner.invoke(app, ["evict", "c-001", "--intent"])
    assert result.exit_code == 0
    assert "skip on re-injection" in result.output
    log = cli_env / "logs" / "events.log.jsonl"
    text = log.read_text()
    assert '"intent":"user-evict"' in text


def test_evict_resolves_basename_query(cli_env: Path) -> None:
    """`recall runtime evict postgres-locking` resolves to a concrete id."""
    from runtime.core.events import EVENT_LOG_SCHEMA_VERSION, EventRecord, append_event
    from runtime.core.manifest import InjectionItemSnapshot

    log = cli_env / "logs" / "events.log.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    snap = InjectionItemSnapshot(
        id="c-pg-001", bucket="hot",
        source_path="hot/lessons/postgres-locking.md",
        sha256="0" * 64, token_count=200, retrieval_reason="r",
        last_touched_turn=0, pinned=False, score=0.0,
    )
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=1, event="SessionStart", session_id="s", turn=0))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=2, event="UserPromptSubmit", session_id="s", turn=1))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=3, event="PostToolUse", session_id="s", turn=1, items_added=[snap]))

    runner = CliRunner()
    result = runner.invoke(app, ["evict", "postgres-locking"])
    assert result.exit_code == 0
    assert "c-pg-001" in result.output
    assert "postgres-locking.md" in result.output


def test_evict_resolves_id_prefix(cli_env: Path) -> None:
    from runtime.core.events import EVENT_LOG_SCHEMA_VERSION, EventRecord, append_event
    from runtime.core.manifest import InjectionItemSnapshot

    log = cli_env / "logs" / "events.log.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    snap = InjectionItemSnapshot(
        id="c-77ab19d3", bucket="hot", source_path="x.md",
        sha256="0" * 64, token_count=100, retrieval_reason="r",
        last_touched_turn=0, pinned=False, score=0.0,
    )
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=1, event="SessionStart", session_id="s", turn=0))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=2, event="UserPromptSubmit", session_id="s", turn=1))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=3, event="PostToolUse", session_id="s", turn=1, items_added=[snap]))

    runner = CliRunner()
    result = runner.invoke(app, ["evict", "c-77ab"])
    assert result.exit_code == 0
    assert "c-77ab19d3" in result.output


def test_evict_ambiguous_query_lists_candidates(cli_env: Path) -> None:
    from runtime.core.events import EVENT_LOG_SCHEMA_VERSION, EventRecord, append_event
    from runtime.core.manifest import InjectionItemSnapshot

    log = cli_env / "logs" / "events.log.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    snaps = [
        InjectionItemSnapshot(id="c-a", bucket="hot", source_path="a/postgres-locking.md", sha256="0" * 64, token_count=100, retrieval_reason="r", last_touched_turn=0, pinned=False, score=0.0),
        InjectionItemSnapshot(id="c-b", bucket="hot", source_path="b/postgres-deadlock.md", sha256="0" * 64, token_count=100, retrieval_reason="r", last_touched_turn=0, pinned=False, score=0.0),
    ]
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=1, event="SessionStart", session_id="s", turn=0))
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=2, event="UserPromptSubmit", session_id="s", turn=1))
    for s in snaps:
        append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=3, event="PostToolUse", session_id="s", turn=1, items_added=[s]))

    runner = CliRunner()
    result = runner.invoke(app, ["evict", "postgres"])
    assert result.exit_code != 0
    assert "be more specific" in result.output
    assert "c-a" in result.output and "c-b" in result.output


def test_evict_no_match_errors(cli_env: Path) -> None:
    _populate_log(cli_env)
    runner = CliRunner()
    result = runner.invoke(app, ["evict", "nothing-matches-this"])
    assert result.exit_code != 0
    assert "no items match" in result.output


def test_add_resolves_query_under_brain_root(cli_env: Path, tmp_path: Path) -> None:
    """`recall runtime add postgres-locking --brain-root <tmp>` finds the lesson."""
    brain = tmp_path / "brain"
    (brain / "semantic" / "lessons").mkdir(parents=True)
    target = brain / "semantic" / "lessons" / "postgres-locking.md"
    target.write_text("use SELECT FOR UPDATE SKIP LOCKED")

    runner = CliRunner()
    result = runner.invoke(app, [
        "add", "postgres-locking",
        "--brain-root", str(brain),
    ])
    assert result.exit_code == 0
    assert "added:" in result.output
    log = cli_env / "logs" / "events.log.jsonl"
    text = log.read_text()
    assert '"intent":"user-add"' in text
    assert "postgres-locking.md" in text


def test_add_no_match_errors(cli_env: Path, tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    brain.mkdir()
    runner = CliRunner()
    result = runner.invoke(app, ["add", "does-not-exist", "--brain-root", str(brain)])
    assert result.exit_code != 0
    assert "no file matches" in result.output


def test_add_command_writes_event_and_content_file(cli_env: Path, tmp_path: Path) -> None:
    """`recall runtime add <path>` reads file, writes AddItem event, stashes content."""
    src = tmp_path / "lesson.md"
    src.write_text("use SELECT FOR UPDATE SKIP LOCKED for the deadlock fix")
    runner = CliRunner()
    result = runner.invoke(app, ["add", str(src)])
    assert result.exit_code == 0
    assert "added:" in result.output
    assert "tok" in result.output
    log = cli_env / "logs" / "events.log.jsonl"
    text = log.read_text()
    assert '"intent":"user-add"' in text
    assert '"tool_name":"user-add"' in text
    # Content stash file
    content_dir = cli_env / "logs" / "added"
    files = list(content_dir.glob("c-*.txt"))
    assert len(files) == 1
    assert "SELECT FOR UPDATE" in files[0].read_text()


def test_add_command_warns_when_reinjection_disabled(cli_env: Path, tmp_path: Path) -> None:
    src = tmp_path / "x.md"
    src.write_text("hello")
    runner = CliRunner()
    result = runner.invoke(app, ["add", str(src)])
    # Default config has enable_reinjection=False
    assert "re-injection is disabled" in result.output


# ---------- recall runtime tail ----------

def test_tail_no_log_friendly(cli_env: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["tail"])
    assert result.exit_code == 0
    assert "no events" in result.output.lower()


def test_tail_shows_recent_events_in_plain_english(cli_env: Path) -> None:
    _populate_log(cli_env)
    runner = CliRunner()
    result = runner.invoke(app, ["tail"])
    assert result.exit_code == 0
    assert "SessionStart" in result.output
    assert "UserPromptSubmit" in result.output
    assert "PostToolUse" in result.output


def test_tail_default_n_is_10(cli_env: Path) -> None:
    """Default shows last 10 events."""
    from runtime.core.events import EVENT_LOG_SCHEMA_VERSION, EventRecord, append_event

    log = cli_env / "logs" / "events.log.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=1, event="SessionStart", session_id="s", turn=0))
    for i in range(20):
        append_event(log, EventRecord(EVENT_LOG_SCHEMA_VERSION, ts_ms=2 + i, event="Stop", session_id="s", turn=0))

    runner = CliRunner()
    result = runner.invoke(app, ["tail"])
    assert result.exit_code == 0
    # Should print exactly 10 'Stop' lines (the last 10 events)
    assert result.output.count("Stop") == 10


def test_tail_custom_n(cli_env: Path) -> None:
    _populate_log(cli_env)
    runner = CliRunner()
    result = runner.invoke(app, ["tail", "1"])
    assert result.exit_code == 0
    # Just one line of meaningful content (plus blank lines may exist)
    non_blank = [l for l in result.output.splitlines() if l.strip()]
    assert len(non_blank) == 1


def test_tail_intent_marker_visible(cli_env: Path, tmp_path: Path) -> None:
    """tail surfaces the [intent=user-add] marker on user-driven events."""
    src = tmp_path / "x.md"
    src.write_text("hello")
    runner = CliRunner()
    runner.invoke(app, ["add", str(src)])
    result = runner.invoke(app, ["tail", "5"])
    assert "intent=user-add" in result.output


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
