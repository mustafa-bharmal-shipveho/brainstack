"""Tests for the global Claude Code hook precedence model.

The wrapper at hooks/agentic_post_tool_global.py decides whether the global
hook fires for a given tool call. Three modes:

1. Default: write to ~/.agent/memory/episodic/AGENT_LEARNINGS.jsonl
2. BRAIN_ROOT env override: respect a custom brain location
3. .agent-local-override file in $CLAUDE_PROJECT_DIR: skip (project's own
   hooks handle it; prevents double-logging if the project has its own
   upstream agentic-stack `.agent/` folder)
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = REPO_ROOT / "agent" / "harness" / "hooks" / "agentic_post_tool_global.py"


def make_brain(root: Path) -> Path:
    """Create a minimal brain at root with episodic dir + harness/hooks."""
    (root / "memory" / "episodic").mkdir(parents=True, exist_ok=True)
    # Copy real hook script tree so the wrapper can dispatch into it
    src_hooks = REPO_ROOT / "agent" / "harness" / "hooks"
    src_harness = REPO_ROOT / "agent" / "harness"
    dst_harness = root / "harness"
    dst_harness.mkdir(parents=True, exist_ok=True)
    # Copy salience.py and text.py (top of harness/)
    for f in ("salience.py", "text.py"):
        (dst_harness / f).write_text((src_harness / f).read_text())
    dst_hooks = dst_harness / "hooks"
    dst_hooks.mkdir(parents=True, exist_ok=True)
    for f in src_hooks.iterdir():
        if f.is_file() and f.suffix == ".py":
            (dst_hooks / f.name).write_text(f.read_text())
    return root


def run_wrapper(tool_payload: dict, env_overrides: dict = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(WRAPPER)],
        input=json.dumps(tool_payload),
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.fixture
def sample_payload():
    return {
        "session_id": "test-session-1",
        "tool_name": "Bash",
        "tool_input": {"command": "ls -la"},
        "tool_response": {"output": "file1\nfile2\n", "exit_code": 0, "error": ""},
    }


def test_default_mode_writes_to_brain_root(tmp_path, sample_payload):
    """With BRAIN_ROOT env set, hook writes to that brain's episodic JSONL."""
    brain = make_brain(tmp_path / "brain")
    project = tmp_path / "project"
    project.mkdir()

    result = run_wrapper(
        sample_payload,
        env_overrides={"BRAIN_ROOT": str(brain), "CLAUDE_PROJECT_DIR": str(project)},
    )
    assert result.returncode == 0, f"hook failed: {result.stderr}"

    episodic = brain / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl"
    assert episodic.exists(), "expected episodic JSONL at BRAIN_ROOT"
    content = episodic.read_text().strip()
    assert content, "episodic JSONL should have at least one entry"
    # Each line should parse as JSON
    lines = content.splitlines()
    for line in lines:
        json.loads(line)


def test_local_override_skips_global_hook(tmp_path, sample_payload):
    """`.agent-local-override` in project dir → wrapper exits 0 without writing."""
    brain = make_brain(tmp_path / "brain")
    project = tmp_path / "project"
    project.mkdir()
    # Create override marker
    (project / ".agent-local-override").touch()

    result = run_wrapper(
        sample_payload,
        env_overrides={"BRAIN_ROOT": str(brain), "CLAUDE_PROJECT_DIR": str(project)},
    )
    assert result.returncode == 0, "wrapper should exit 0 when overridden"

    episodic = brain / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl"
    # Either the file doesn't exist or it's empty — either way, no entry written
    if episodic.exists():
        assert not episodic.read_text().strip(), (
            "expected no episodic write when .agent-local-override is present"
        )


def test_no_brain_root_falls_back_to_home_agent(tmp_path, sample_payload, monkeypatch):
    """Without BRAIN_ROOT set, wrapper resolves brain to ~/.agent/.

    We point HOME at a tmp path so we can verify without touching the user's
    real ~/.agent/ during tests.
    """
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    brain = make_brain(fake_home / ".agent")
    project = tmp_path / "project"
    project.mkdir()

    # Override HOME via env so os.path.expanduser("~") resolves to fake_home
    result = run_wrapper(
        sample_payload,
        env_overrides={
            "HOME": str(fake_home),
            "CLAUDE_PROJECT_DIR": str(project),
            # Explicitly clear BRAIN_ROOT in case the parent shell has it
            "BRAIN_ROOT": "",
        },
    )
    assert result.returncode == 0, f"hook failed: {result.stderr}"

    episodic = brain / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl"
    assert episodic.exists(), "expected episodic write to ~/.agent/ via HOME fallback"
    assert episodic.read_text().strip(), "expected at least one episodic entry"


def test_failure_payload_still_logs(tmp_path):
    """A failed Bash invocation (exit_code != 0) should still get logged."""
    brain = make_brain(tmp_path / "brain")
    project = tmp_path / "project"
    project.mkdir()

    fail_payload = {
        "session_id": "test-session-2",
        "tool_name": "Bash",
        "tool_input": {"command": "false"},
        "tool_response": {"output": "", "exit_code": 1, "error": "command failed"},
    }
    result = run_wrapper(
        fail_payload,
        env_overrides={"BRAIN_ROOT": str(brain), "CLAUDE_PROJECT_DIR": str(project)},
    )
    assert result.returncode == 0, f"hook should not propagate tool failures: {result.stderr}"

    episodic = brain / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl"
    assert episodic.exists()
