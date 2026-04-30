"""Tests for the Codex CLI adapter (`agent/tools/codex_adapter.py`).

PR-C of the multi-tool migration series. Ingests Codex CLI session
rollouts and command history into the brain's episodic stream under a
per-tool `codex` namespace at
`<brain>/memory/episodic/codex/AGENT_LEARNINGS.jsonl`.

Source layout (Codex CLI, real shape on disk):
  ~/.codex/
    sessions/
      <YYYY>/<MM>/<DD>/
        rollout-<timestamp>-<session-id>.jsonl   # one event per line
    history.jsonl                                # search/command history
    config.toml, state_*.sqlite                  # skipped (not memory)

Each rollout line:
  {"type": "session_meta"|"event_msg"|"response_item"|..., "timestamp": "...", "payload": {...}}

Each history line:
  {"session_id": "...", "text": "...", "ts": <unix ms>}

Migrated as brainstack episodes with `origin: "codex.cli.<type>"` and the
same `timestamp` / `action` / `detail` / `skill: "codex-cli"` shape every
other episode in the brain has.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "tools"))
sys.path.insert(0, str(REPO_ROOT / "agent" / "memory"))

from migrate_dispatcher import (  # noqa: E402
    NoAdapterError,
    detect_format,
    dispatch,
    get_adapter_for,
    registered_adapters,
)


def _make_codex_source(root: Path, sessions: dict[str, list[dict]] | None = None,
                       history: list[dict] | None = None) -> Path:
    """Build a synthetic ~/.codex/-shaped source for testing."""
    sessions = sessions or {}
    history = history or []
    root.mkdir(parents=True, exist_ok=True)
    # Codex CLI's signature combo: history.jsonl + config.toml
    (root / "config.toml").write_text("# fake config\n")

    for rel_path, lines in sessions.items():
        path = root / "sessions" / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")

    if history:
        (root / "history.jsonl").write_text(
            "\n".join(json.dumps(h) for h in history) + "\n"
        )

    return root


# Minimal realistic samples — keys here mirror what's actually in the
# user's `~/.codex/sessions/.../rollout-*.jsonl` (verified on disk).
SAMPLE_SESSION_META = {
    "type": "session_meta",
    "timestamp": "2026-04-29T10:00:00Z",
    "payload": {
        "id": "019dded3-2c2b-77d0-b6bf-545c92cdd4ad",
        "cli_version": "0.125.0",
        "model_provider": "openai",
        "cwd": "/home/user/repo",
    },
}

SAMPLE_RESPONSE_ITEM = {
    "type": "response_item",
    "timestamp": "2026-04-29T10:00:05Z",
    "payload": {
        "role": "user",
        "content": "Help me debug this test failure",
        "type": "message",
    },
}

SAMPLE_EVENT_MSG = {
    "type": "event_msg",
    "timestamp": "2026-04-29T10:00:06Z",
    "payload": {
        "type": "session_started",
        "turn_id": "abc123",
        "model_context_window": 200000,
    },
}

SAMPLE_HISTORY = {
    "session_id": "019dded3-2c2b-77d0-b6bf-545c92cdd4ad",
    "text": "fix the test",
    # Codex CLI's history.jsonl uses unix-SECONDS (not ms). Real example
    # from `~/.codex/history.jsonl`: ts=1776965620 ≈ 2026-04-22.
    "ts": 1776965620,
}


# --- Registration + format support ---


def test_codex_adapter_registered_on_import():
    assert "codex-cli" in registered_adapters(), \
        f"codex-cli adapter not registered; got {registered_adapters()}"
    assert get_adapter_for("codex-cli") is not None


def test_codex_adapter_supports_format():
    adapter = get_adapter_for("codex-cli")
    assert adapter is not None
    assert adapter.supports("codex-cli")
    assert not adapter.supports("claude-code-flat")
    assert not adapter.supports("cursor-plans")


# --- Dispatch routing ---


def test_codex_dispatch_dry_run_counts(tmp_path):
    src = _make_codex_source(
        tmp_path / "codex",
        sessions={
            "2026/04/29/rollout-x.jsonl": [SAMPLE_SESSION_META, SAMPLE_RESPONSE_ITEM],
            "2026/04/30/rollout-y.jsonl": [SAMPLE_EVENT_MSG],
        },
        history=[SAMPLE_HISTORY],
    )
    result = dispatch(src=src, dst=tmp_path / "brain", dry_run=True)
    assert result.format == "codex-cli"
    assert result.dry_run is True
    # 2 session_meta+response_item + 1 event_msg + 1 history = 4 episodes
    assert result.files_planned >= 1
    assert result.tool_specific.get("episodes_planned") == 4
    assert result.tool_specific.get("rollouts_planned") == 2
    assert result.tool_specific.get("history_planned") == 1
    # No writes during dry-run
    assert not (tmp_path / "brain" / "memory" / "episodic" / "codex").exists()


def test_codex_dispatch_executes(tmp_path):
    src = _make_codex_source(
        tmp_path / "codex",
        sessions={
            "2026/04/29/rollout-x.jsonl": [SAMPLE_SESSION_META, SAMPLE_RESPONSE_ITEM],
        },
        history=[SAMPLE_HISTORY],
    )
    result = dispatch(src=src, dst=tmp_path / "brain", dry_run=False)
    assert result.format == "codex-cli"
    assert result.dry_run is False
    assert result.tool_specific.get("episodes_imported") == 3  # 2 + 1 history
    assert result.tool_specific.get("rollouts_imported") == 1

    # Episodic JSONL was written under the codex namespace
    epi = tmp_path / "brain" / "memory" / "episodic" / "codex" / "AGENT_LEARNINGS.jsonl"
    assert epi.exists(), f"codex episodic missing; brain layout: {sorted((tmp_path/'brain'/'memory').rglob('*'))}"
    rows = [json.loads(line) for line in epi.read_text().strip().splitlines()]
    assert len(rows) == 3
    # Each row is a brainstack-shaped episode
    for r in rows:
        assert r["skill"] == "codex-cli"
        assert "timestamp" in r
        assert r["origin"].startswith("codex.cli.")


def test_codex_episode_origin_per_event_type(tmp_path):
    """Each event type gets a distinct `origin` so cluster.py groups them."""
    src = _make_codex_source(
        tmp_path / "codex",
        sessions={
            "2026/04/29/rollout-x.jsonl": [SAMPLE_SESSION_META, SAMPLE_RESPONSE_ITEM, SAMPLE_EVENT_MSG],
        },
    )
    dispatch(src=src, dst=tmp_path / "brain", dry_run=False)
    epi = tmp_path / "brain" / "memory" / "episodic" / "codex" / "AGENT_LEARNINGS.jsonl"
    rows = [json.loads(line) for line in epi.read_text().strip().splitlines()]
    origins = {r["origin"] for r in rows}
    assert origins == {
        "codex.cli.session_meta",
        "codex.cli.response_item",
        "codex.cli.event_msg",
    }


def test_codex_history_origin(tmp_path):
    src = _make_codex_source(
        tmp_path / "codex",
        sessions={},
        history=[SAMPLE_HISTORY, {**SAMPLE_HISTORY, "text": "another command"}],
    )
    dispatch(src=src, dst=tmp_path / "brain", dry_run=False)
    epi = tmp_path / "brain" / "memory" / "episodic" / "codex" / "AGENT_LEARNINGS.jsonl"
    rows = [json.loads(line) for line in epi.read_text().strip().splitlines()]
    assert len(rows) == 2
    assert all(r["origin"] == "codex.cli.history" for r in rows)
    assert {r["action"] for r in rows} == {"codex history command"}
    # `text` ends up in detail
    details = [r["detail"] for r in rows]
    assert any("fix the test" in d for d in details)


def test_codex_idempotent(tmp_path):
    """Re-running the migrate against the same source must not duplicate
    rows. Idempotency is tracked by file-content hash in the sidecar."""
    src = _make_codex_source(
        tmp_path / "codex",
        sessions={"2026/04/29/rollout-x.jsonl": [SAMPLE_RESPONSE_ITEM]},
        history=[SAMPLE_HISTORY],
    )
    dispatch(src=src, dst=tmp_path / "brain", dry_run=False)
    epi = tmp_path / "brain" / "memory" / "episodic" / "codex" / "AGENT_LEARNINGS.jsonl"
    first_rows = epi.read_text().strip().splitlines()

    # Run again
    result2 = dispatch(src=src, dst=tmp_path / "brain", dry_run=False)
    second_rows = epi.read_text().strip().splitlines()
    assert first_rows == second_rows, \
        f"re-run produced duplicate episodes: was {len(first_rows)}, now {len(second_rows)}"
    # And the result reports zero new imports
    assert result2.tool_specific.get("episodes_imported") == 0


def test_codex_idempotent_imports_only_new_rollouts(tmp_path):
    """If the source grows between runs (new rollout file appears), the
    second run imports only the new file's events."""
    src_root = tmp_path / "codex"
    _make_codex_source(
        src_root,
        sessions={"2026/04/29/rollout-x.jsonl": [SAMPLE_RESPONSE_ITEM]},
    )
    result1 = dispatch(src=src_root, dst=tmp_path / "brain", dry_run=False)
    assert result1.tool_specific.get("episodes_imported") == 1

    # New rollout file appears
    new_rollout = src_root / "sessions" / "2026" / "04" / "30" / "rollout-y.jsonl"
    new_rollout.parent.mkdir(parents=True, exist_ok=True)
    new_rollout.write_text(json.dumps(SAMPLE_EVENT_MSG) + "\n")

    result2 = dispatch(src=src_root, dst=tmp_path / "brain", dry_run=False)
    assert result2.tool_specific.get("episodes_imported") == 1, \
        f"expected only the new rollout to be imported; got {result2.tool_specific}"


def test_codex_skips_torn_jsonl_lines(tmp_path):
    """A rollout file mid-write may have a malformed last line. The
    adapter should skip the bad line, count the good ones, and continue."""
    src = tmp_path / "codex"
    src.mkdir()
    (src / "config.toml").write_text("# fake\n")
    sess = src / "sessions" / "2026" / "04" / "29"
    sess.mkdir(parents=True)
    (sess / "rollout-x.jsonl").write_text(
        json.dumps(SAMPLE_SESSION_META) + "\n"
        + json.dumps(SAMPLE_RESPONSE_ITEM) + "\n"
        + "{this is not valid json\n"
    )
    result = dispatch(src=src, dst=tmp_path / "brain", dry_run=False)
    # 2 valid, 1 skipped
    assert result.tool_specific.get("episodes_imported") == 2
    assert any("malformed" in w.lower() or "skip" in w.lower() for w in result.warnings)


def test_codex_namespace_default(tmp_path):
    src = _make_codex_source(
        tmp_path / "codex",
        sessions={"2026/04/29/rollout-x.jsonl": [SAMPLE_RESPONSE_ITEM]},
    )
    result = dispatch(src=src, dst=tmp_path / "brain", dry_run=False)
    assert result.namespace == "codex"
    # Physical path is under episodic/codex/
    assert (tmp_path / "brain" / "memory" / "episodic" / "codex" / "AGENT_LEARNINGS.jsonl").exists()


def test_codex_skips_sqlite_and_config(tmp_path):
    """Adapter must NOT migrate state SQLite or config files."""
    src = _make_codex_source(
        tmp_path / "codex",
        sessions={"2026/04/29/rollout-x.jsonl": [SAMPLE_RESPONSE_ITEM]},
    )
    (src / "state_5.sqlite").write_bytes(b"fake sqlite blob")
    (src / "logs_2.sqlite").write_bytes(b"fake logs")

    dispatch(src=src, dst=tmp_path / "brain", dry_run=False)
    # No SQLite content in target
    for path in (tmp_path / "brain" / "memory").rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            assert b"fake sqlite blob" not in content
            assert b"fake logs" not in content


# --- Real-data sanity check ---


def test_codex_history_ts_seconds_to_iso(tmp_path):
    """Codex review P1: `ts` in history.jsonl is unix-SECONDS, not ms.
    Earlier `ts/1000` divide produced 1970-era timestamps. Verify a
    realistic 2026 ts produces a 2026 episode."""
    src = _make_codex_source(
        tmp_path / "codex",
        history=[{
            "session_id": "x", "text": "do thing",
            "ts": 1776965620,  # ~2026-04-22 in seconds
        }],
    )
    dispatch(src=src, dst=tmp_path / "brain", dry_run=False)
    epi = tmp_path / "brain" / "memory" / "episodic" / "codex" / "AGENT_LEARNINGS.jsonl"
    rows = [json.loads(line) for line in epi.read_text().strip().splitlines()]
    assert len(rows) == 1
    assert rows[0]["timestamp"].startswith("2026-"), \
        f"history ts converted wrong; got {rows[0]['timestamp']!r} (1970 means seconds-vs-ms confusion)"


def test_codex_history_ts_milliseconds_also_works(tmp_path):
    """Detection heuristic: 13-digit (ms) ALSO maps to a 2026 timestamp."""
    src = _make_codex_source(
        tmp_path / "codex",
        history=[{
            "session_id": "x", "text": "thing",
            "ts": 1776965620000,  # same instant in ms
        }],
    )
    dispatch(src=src, dst=tmp_path / "brain", dry_run=False)
    epi = tmp_path / "brain" / "memory" / "episodic" / "codex" / "AGENT_LEARNINGS.jsonl"
    rows = [json.loads(line) for line in epi.read_text().strip().splitlines()]
    assert rows[0]["timestamp"].startswith("2026-")


def test_codex_history_idempotent_under_appended_lines(tmp_path):
    """Codex review P2: history.jsonl is append-only — when a new line
    is appended between runs, only the NEW line should be imported, not
    the whole prior content again. The earlier whole-file-hash sidecar
    failed this; offset-tracking fixes it."""
    src_root = tmp_path / "codex"
    _make_codex_source(
        src_root,
        history=[
            {"session_id": "s1", "text": "first", "ts": 1776965620},
            {"session_id": "s1", "text": "second", "ts": 1776965621},
        ],
    )
    result1 = dispatch(src=src_root, dst=tmp_path / "brain", dry_run=False)
    assert result1.tool_specific.get("episodes_imported") == 2

    # Append a NEW history line
    history_path = src_root / "history.jsonl"
    with history_path.open("a") as f:
        f.write(json.dumps({"session_id": "s1", "text": "third", "ts": 1776965622}) + "\n")

    result2 = dispatch(src=src_root, dst=tmp_path / "brain", dry_run=False)
    assert result2.tool_specific.get("episodes_imported") == 1, \
        f"appended line should produce ONE new episode; got {result2.tool_specific}"

    # Total in target = 3 (not 5)
    epi = tmp_path / "brain" / "memory" / "episodic" / "codex" / "AGENT_LEARNINGS.jsonl"
    rows = [json.loads(line) for line in epi.read_text().strip().splitlines()]
    assert len(rows) == 3, \
        f"appended line caused duplicate import; expected 3 rows, got {len(rows)}"
    # All three texts present, in order
    assert [r["detail"] for r in rows] == ["first", "second", "third"]


@pytest.mark.skipif(
    not Path.home().joinpath(".codex/sessions").is_dir(),
    reason="user's ~/.codex/sessions/ doesn't exist on this machine",
)
def test_codex_real_data_dry_run():
    """Dry-run against the user's actual ~/.codex/."""
    src = Path.home() / ".codex"
    rollouts = list((src / "sessions").rglob("rollout-*.jsonl"))
    if not rollouts:
        pytest.skip("user has no rollout files")

    import tempfile
    with tempfile.TemporaryDirectory(prefix="brainstack-codex-realdata-") as tmp:
        dst = Path(tmp)
        result = dispatch(src=src, dst=dst, dry_run=True)
        assert result.format == "codex-cli"
        assert result.tool_specific.get("rollouts_planned") == len(rollouts), \
            f"plan rollout count mismatch: planned={result.tool_specific.get('rollouts_planned')}, on disk={len(rollouts)}"
