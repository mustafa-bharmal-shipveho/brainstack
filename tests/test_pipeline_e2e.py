"""End-to-end pipeline test.

Walks the full chain that a real Claude Code session triggers:

  1. Tool call → PostToolUse hook captures Bash command + output
  2. Hook appends a row to memory/episodic/AGENT_LEARNINGS.jsonl
  3. (Time passes; the row sits in the JSONL with a secret in it)
  4. sync.sh runs redact_jsonl.py → secret is replaced with [REDACTED:...]
  5. sync.sh runs the redact.py pre-commit scanner → no remaining hits
  6. (sync.sh would commit + push, but we stop short of that here)

If any step misses a secret that the next would catch, that's still a
pass — defense in depth. But the e2e test asserts that by the end of
step 4, the JSONL file is clean of secrets, and step 5's scanner finds
no remaining hits.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = REPO_ROOT / "agent" / "harness" / "hooks" / "agentic_post_tool_global.py"
JSONL_SCRUBBER = REPO_ROOT / "agent" / "tools" / "redact_jsonl.py"
REDACT = REPO_ROOT / "agent" / "tools" / "redact.py"


def make_brain(brain: Path) -> None:
    """Build a minimal brain at the given path. Mirrors install.sh's layout."""
    (brain / "memory" / "episodic").mkdir(parents=True)
    (brain / "memory" / "working").mkdir()
    (brain / "memory" / "candidates").mkdir()
    (brain / "memory" / "semantic" / "lessons").mkdir(parents=True)
    (brain / "memory" / "personal" / "notes").mkdir(parents=True)
    (brain / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl").touch()

    src_hooks = REPO_ROOT / "agent" / "harness" / "hooks"
    src_harness = REPO_ROOT / "agent" / "harness"
    dst_harness = brain / "harness"
    dst_harness.mkdir()
    for f in ("salience.py", "text.py"):
        (dst_harness / f).write_text((src_harness / f).read_text())
    dst_hooks = dst_harness / "hooks"
    dst_hooks.mkdir()
    for f in src_hooks.iterdir():
        if f.is_file() and f.suffix == ".py":
            (dst_hooks / f.name).write_text(f.read_text())

    dst_tools = brain / "tools"
    dst_tools.mkdir()
    for f in (REPO_ROOT / "agent" / "tools").iterdir():
        if f.is_file():
            (dst_tools / f.name).write_text(f.read_text())


def fire_hook(brain: Path, fake_home: Path, project: Path, payload: dict) -> None:
    env = os.environ.copy()
    env.update({
        "HOME": str(fake_home),
        "BRAIN_ROOT": str(brain),
        "CLAUDE_PROJECT_DIR": str(project),
    })
    subprocess.run(
        [sys.executable, str(WRAPPER)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def run(*args: str, **env: str) -> subprocess.CompletedProcess:
    e = os.environ.copy()
    e.update(env)
    return subprocess.run(
        [sys.executable, *args],
        capture_output=True,
        text=True,
        env=e,
    )


def test_full_pipeline_scrubs_aws_key_from_bash_capture(tmp_path):
    """A Bash tool call that exfiltrates an AWS key should be scrubbed before push."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    brain = fake_home / ".agent"
    make_brain(brain)
    project = tmp_path / "proj"
    project.mkdir()

    # Step 1+2: a tool call captures a Bash command containing an AWS key
    payload = {
        "session_id": "e2e-1",
        "tool_name": "Bash",
        "tool_input": {
            "command": "export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE; aws s3 ls",
        },
        "tool_response": {
            "output": "2026-04-27 03:14:15 my-bucket\n",
            "exit_code": 0,
            "error": "",
        },
    }
    fire_hook(brain, fake_home, project, payload)

    # JSONL should now have the row, with the secret in it (pre-redaction)
    jsonl = brain / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl"
    assert jsonl.exists() and jsonl.stat().st_size > 0
    raw = jsonl.read_text()
    assert "AKIAIOSFODNN7EXAMPLE" in raw, (
        "pre-redaction state should contain the captured secret; "
        f"hook capture broken? raw: {raw!r}"
    )

    # Step 4: sync-time scrubber runs
    r = run(str(JSONL_SCRUBBER), str(brain / "memory" / "episodic"))
    assert r.returncode == 1, f"scrubber should report changes (rc=1); got {r.returncode}, stderr={r.stderr}"

    # Verify the JSONL no longer has the literal secret
    after = jsonl.read_text()
    assert "AKIAIOSFODNN7EXAMPLE" not in after, (
        f"secret survived the scrubber; final JSONL: {after!r}"
    )
    # And the [REDACTED:...] marker took its place
    assert "[REDACTED:" in after

    # Step 5: pre-commit scanner finds nothing now
    r2 = run(str(REDACT), "--no-entropy", str(brain / "memory" / "episodic"))
    assert r2.returncode == 0, (
        f"pre-commit scanner should find no remaining hits; stdout={r2.stdout}"
    )

    # And the JSONL is still parseable line-by-line
    for line in after.splitlines():
        if line.strip():
            json.loads(line)


def test_pipeline_idempotent_after_first_scrub(tmp_path):
    """Running the scrubber a second time on a clean JSONL is a no-op."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    brain = fake_home / ".agent"
    make_brain(brain)
    project = tmp_path / "proj"
    project.mkdir()

    payload = {
        "session_id": "e2e-2",
        "tool_name": "Bash",
        "tool_input": {"command": "echo ghp_abcdefghijklmnopqrstuvwxyz0123456789"},
        "tool_response": {"output": "ghp_abcdefghijklmnopqrstuvwxyz0123456789\n", "exit_code": 0, "error": ""},
    }
    fire_hook(brain, fake_home, project, payload)

    r1 = run(str(JSONL_SCRUBBER), str(brain / "memory" / "episodic"))
    assert r1.returncode == 1

    r2 = run(str(JSONL_SCRUBBER), str(brain / "memory" / "episodic"))
    assert r2.returncode == 0, (
        f"second scrubber run should be a no-op; rc={r2.returncode}, stdout={r2.stdout}"
    )


def test_pipeline_scrubs_edit_old_new_strings(tmp_path):
    """An Edit tool call where old_string/new_string contain a secret gets scrubbed."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    brain = fake_home / ".agent"
    make_brain(brain)
    project = tmp_path / "proj"
    project.mkdir()

    payload = {
        "session_id": "e2e-edit",
        "tool_name": "Edit",
        "tool_input": {
            "file_path": "/tmp/conf.env",
            "old_string": "AWS_KEY=AKIAIOSFODNN7EXAMPLE",
            "new_string": "AWS_KEY=AKIA0000000000000000",
        },
        "tool_response": {"output": "ok", "exit_code": 0, "error": ""},
    }
    fire_hook(brain, fake_home, project, payload)

    jsonl = brain / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl"
    pre = jsonl.read_text()
    assert "AKIAIOSFODNN7EXAMPLE" in pre, (
        "hook should have captured Edit old_string verbatim"
    )

    run(str(JSONL_SCRUBBER), str(brain / "memory" / "episodic"))

    post = jsonl.read_text()
    assert "AKIAIOSFODNN7EXAMPLE" not in post
    # The replacement key (which still matches AKIA but is intentionally a
    # placeholder) should also be scrubbed
    assert "AKIA0000000000000000" not in post


def test_pipeline_handles_authorization_bearer_in_curl(tmp_path):
    """A captured curl with an Authorization: Bearer header should get scrubbed."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    brain = fake_home / ".agent"
    make_brain(brain)
    project = tmp_path / "proj"
    project.mkdir()

    fake_pat = "ghp_" + "A" * 36
    payload = {
        "session_id": "e2e-3",
        "tool_name": "Bash",
        "tool_input": {
            "command": f"curl -H 'Authorization: Bearer {fake_pat}' https://api.example.com/me",
        },
        "tool_response": {"output": '{"login":"alice"}\n', "exit_code": 0, "error": ""},
    }
    fire_hook(brain, fake_home, project, payload)

    jsonl = brain / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl"
    pre = jsonl.read_text()
    assert fake_pat in pre, "hook should have captured the Bearer token"

    run(str(JSONL_SCRUBBER), str(brain / "memory" / "episodic"))

    post = jsonl.read_text()
    assert fake_pat not in post, "scrubber should have replaced the token"
