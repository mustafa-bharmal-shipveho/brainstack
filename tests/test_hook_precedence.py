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
    # The wrapper now validates BRAIN_ROOT lies under $HOME for security.
    # Pin HOME to the test root so the validation passes.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    brain = make_brain(fake_home / "brain")
    project = tmp_path / "project"
    project.mkdir()

    result = run_wrapper(
        sample_payload,
        env_overrides={
            "HOME": str(fake_home),
            "BRAIN_ROOT": str(brain),
            "CLAUDE_PROJECT_DIR": str(project),
        },
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
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    brain = make_brain(fake_home / "brain")
    project = tmp_path / "project"
    project.mkdir()
    # Create override marker
    (project / ".agent-local-override").touch()

    result = run_wrapper(
        sample_payload,
        env_overrides={
            "HOME": str(fake_home),
            "BRAIN_ROOT": str(brain),
            "CLAUDE_PROJECT_DIR": str(project),
        },
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
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    brain = make_brain(fake_home / "brain")
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
        env_overrides={
            "HOME": str(fake_home),
            "BRAIN_ROOT": str(brain),
            "CLAUDE_PROJECT_DIR": str(project),
        },
    )
    assert result.returncode == 0, f"hook should not propagate tool failures: {result.stderr}"

    episodic = brain / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl"
    assert episodic.exists()


# ----- New: BRAIN_ROOT validation (Fix #3) -----


def test_brain_root_outside_home_is_rejected(tmp_path, sample_payload):
    """BRAIN_ROOT pointing outside $HOME must fall back to default + log warning."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    # Default brain at $HOME/.agent — make it real so the wrapper has somewhere to dispatch
    default_brain = make_brain(fake_home / ".agent")
    # Hostile brain OUTSIDE $HOME
    hostile = tmp_path / "hostile_brain"
    make_brain(hostile)

    project = tmp_path / "project"
    project.mkdir()

    result = run_wrapper(
        sample_payload,
        env_overrides={
            "HOME": str(fake_home),
            "BRAIN_ROOT": str(hostile),
            "CLAUDE_PROJECT_DIR": str(project),
        },
    )
    assert result.returncode == 0
    # Hostile brain should NOT have been written to
    hostile_jsonl = hostile / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl"
    if hostile_jsonl.exists():
        assert not hostile_jsonl.read_text().strip(), (
            "hostile BRAIN_ROOT must not receive episodic writes"
        )
    # Default brain should have received the write
    default_jsonl = default_brain / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl"
    assert default_jsonl.exists() and default_jsonl.read_text().strip(), (
        "fallback should have written to $HOME/.agent"
    )
    # And a warning should have been emitted
    assert "outside" in result.stderr.lower() or "refusing" in result.stderr.lower()


def test_brain_root_without_hook_script_is_rejected(tmp_path, sample_payload):
    """A path under $HOME but missing the hook script must be rejected."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    default_brain = make_brain(fake_home / ".agent")
    # Empty dir under $HOME — no harness/
    empty = fake_home / "fake_brain"
    empty.mkdir()

    project = tmp_path / "project"
    project.mkdir()

    result = run_wrapper(
        sample_payload,
        env_overrides={
            "HOME": str(fake_home),
            "BRAIN_ROOT": str(empty),
            "CLAUDE_PROJECT_DIR": str(project),
        },
    )
    assert result.returncode == 0
    # Default brain should have received the write
    default_jsonl = default_brain / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl"
    assert default_jsonl.exists() and default_jsonl.read_text().strip()


def test_brain_root_with_traversal_is_rejected(tmp_path, sample_payload):
    """Path traversal via .. in BRAIN_ROOT must be rejected after resolution."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    default_brain = make_brain(fake_home / ".agent")
    # tmp_path / ".." resolves outside fake_home (it's tmp_path's parent)
    traversal = fake_home / ".." / "outside"
    (tmp_path / "outside").mkdir()
    make_brain(tmp_path / "outside")

    project = tmp_path / "project"
    project.mkdir()

    result = run_wrapper(
        sample_payload,
        env_overrides={
            "HOME": str(fake_home),
            "BRAIN_ROOT": str(traversal),
            "CLAUDE_PROJECT_DIR": str(project),
        },
    )
    assert result.returncode == 0
    # The traversal target should NOT have been written to
    outside_jsonl = tmp_path / "outside" / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl"
    if outside_jsonl.exists():
        assert not outside_jsonl.read_text().strip(), "traversal target must not receive writes"


def test_local_override_fire_is_logged(tmp_path, sample_payload):
    """When .agent-local-override fires, an entry must land in override.log."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    brain = make_brain(fake_home / ".agent")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".agent-local-override").touch()

    result = run_wrapper(
        sample_payload,
        env_overrides={
            "HOME": str(fake_home),
            "BRAIN_ROOT": str(brain),
            "CLAUDE_PROJECT_DIR": str(project),
        },
    )
    assert result.returncode == 0
    override_log = brain / "override.log"
    assert override_log.exists(), "override.log should be created on fire"
    content = override_log.read_text()
    assert str(project) in content, f"override.log should record project path; got: {content}"


def test_local_override_fire_lands_in_custom_brain_not_default(tmp_path, sample_payload):
    """With a non-default BRAIN_ROOT, override.log must follow that brain.

    Regression test for the bug where _log_override_fire hardcoded
    ~/.agent regardless of the resolved BRAIN_ROOT, so the audit trail
    landed in the wrong place when users ran with a custom brain.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    # Default brain location — should NOT receive the override log
    default_brain = make_brain(fake_home / ".agent")
    # Custom brain — should receive it
    custom_brain = make_brain(fake_home / "custom-brain")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".agent-local-override").touch()

    result = run_wrapper(
        sample_payload,
        env_overrides={
            "HOME": str(fake_home),
            "BRAIN_ROOT": str(custom_brain),
            "CLAUDE_PROJECT_DIR": str(project),
        },
    )
    assert result.returncode == 0
    custom_log = custom_brain / "override.log"
    default_log = default_brain / "override.log"
    assert custom_log.exists(), "override.log should land in BRAIN_ROOT, not ~/.agent"
    assert str(project) in custom_log.read_text()
    if default_log.exists():
        assert str(project) not in default_log.read_text(), (
            "override.log should NOT have been written to ~/.agent when "
            "BRAIN_ROOT pointed elsewhere"
        )


def test_brain_root_under_home_is_accepted(tmp_path, sample_payload):
    """A custom BRAIN_ROOT that lives under $HOME must work."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    custom = make_brain(fake_home / "custom-brain")
    project = tmp_path / "project"
    project.mkdir()

    result = run_wrapper(
        sample_payload,
        env_overrides={
            "HOME": str(fake_home),
            "BRAIN_ROOT": str(custom),
            "CLAUDE_PROJECT_DIR": str(project),
        },
    )
    assert result.returncode == 0
    custom_jsonl = custom / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl"
    assert custom_jsonl.exists() and custom_jsonl.read_text().strip(), (
        "custom BRAIN_ROOT under $HOME should be accepted"
    )
