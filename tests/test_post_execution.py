"""Tests for harness/hooks/post_execution.py — `log_execution` schema stamping.

PR1 adds two parameters to `log_execution`:
  - `origin: str = "coding.tool_call"` — stamps the episode's origin
  - `summary: Optional[str] = None` — when None, derives from
    `(reflection[:120] or action[:120])`. Stamps the summary so the
    cluster pipeline can use it as a clustering feature.
"""
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "harness"))


@pytest.fixture
def isolated_episodic(tmp_path, monkeypatch):
    """Redirect EPISODIC into tmp_path so log_execution doesn't touch the
    real ~/.agent. post_execution.py uses relative imports (from
    ._provenance), which means it must be loaded as a member of the
    `hooks` namespace package (parent dir agent/harness is on sys.path)."""
    from hooks import post_execution  # noqa: WPS433
    target = tmp_path / "AGENT_LEARNINGS.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(post_execution, "EPISODIC", str(target))
    return target


def _read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(l) for l in f.read().splitlines() if l.strip()]


def test_log_execution_stamps_default_origin(isolated_episodic):
    from hooks import post_execution
    post_execution.log_execution(
        skill_name="claude-code",
        action="bash: ls",
        result="ok",
        success=True,
        reflection="ran ls",
    )
    rows = _read_jsonl(isolated_episodic)
    assert len(rows) == 1
    assert rows[0]["origin"] == "coding.tool_call"


def test_log_execution_explicit_origin_passes_through(isolated_episodic):
    from hooks import post_execution
    post_execution.log_execution(
        skill_name="agentry-inbox",
        action="surface_event",
        result="ok",
        success=True,
        reflection="captured slack mention",
        origin="agentry.inbox.surface_event",
    )
    rows = _read_jsonl(isolated_episodic)
    assert rows[0]["origin"] == "agentry.inbox.surface_event"


def test_log_execution_derives_summary_from_reflection(isolated_episodic):
    """When summary is None, fall back to reflection[:120]."""
    from hooks import post_execution
    long_reflection = "x" * 200
    post_execution.log_execution(
        skill_name="claude-code",
        action="bash",
        result="ok",
        success=True,
        reflection=long_reflection,
    )
    rows = _read_jsonl(isolated_episodic)
    assert rows[0]["summary"] == "x" * 120


def test_log_execution_derives_summary_from_action_when_no_reflection(isolated_episodic):
    """No reflection AND no summary → action[:120]."""
    from hooks import post_execution
    post_execution.log_execution(
        skill_name="claude-code",
        action="bash: pnpm install",
        result="ok",
        success=True,
        reflection="",
    )
    rows = _read_jsonl(isolated_episodic)
    assert rows[0]["summary"] == "bash: pnpm install"


def test_log_execution_explicit_summary_passes_through(isolated_episodic):
    """Explicit summary not auto-derived."""
    from hooks import post_execution
    post_execution.log_execution(
        skill_name="claude-code",
        action="bash: ls",
        result="ok",
        success=True,
        reflection="ran ls",
        summary="custom summary text",
    )
    rows = _read_jsonl(isolated_episodic)
    assert rows[0]["summary"] == "custom summary text"


def test_log_execution_keeps_existing_fields(isolated_episodic):
    """origin/summary additions don't break existing entry shape."""
    from hooks import post_execution
    post_execution.log_execution(
        skill_name="claude-code",
        action="bash: ls",
        result="ok",
        success=True,
        reflection="ran ls",
        importance=8,
        confidence=0.9,
        pain_score=4,
    )
    rows = _read_jsonl(isolated_episodic)
    row = rows[0]
    for f in ("timestamp", "skill", "action", "result", "detail",
              "pain_score", "importance", "reflection", "confidence",
              "source", "evidence_ids", "origin", "summary"):
        assert f in row, f"missing field: {f}"
    assert row["importance"] == 8
    assert row["confidence"] == 0.9
    assert row["pain_score"] == 4
