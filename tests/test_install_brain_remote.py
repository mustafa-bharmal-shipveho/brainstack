"""Smoke test for `install.sh --brain-remote <url>`.

Verifies the plug-in flow: a single command installs the brain, initializes
it as a git repo, sets origin to the user-supplied URL, makes the initial
commit, and installs the pre-commit secret-scan hook.

Skip if Python < 3.10 (install.sh requires it) or no python3.10+ on PATH.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"


def _find_py310() -> str | None:
    for cand in ("python3.13", "python3.12", "python3.11", "python3.10"):
        path = shutil.which(cand)
        if path:
            return path
    return None


@pytest.mark.skipif(_find_py310() is None, reason="needs Python >= 3.10 on PATH")
def test_install_with_brain_remote_inits_git_and_commits(tmp_path):
    py = _find_py310()
    sandbox = tmp_path / ".agent"
    fake_url = "git@github.com:example/test-brain.git"

    env = os.environ.copy()
    env["PYTHON_BIN"] = py
    # Use real HOME so the user's git config (user.name/email) is available;
    # only the brain dir is sandboxed via --brain-root.

    r = subprocess.run(
        [str(INSTALL_SH), "--brain-root", str(sandbox), "--brain-remote", fake_url],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert r.returncode == 0, f"install failed: rc={r.returncode}, stderr={r.stderr}"

    # Brain layout exists
    assert (sandbox / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl").exists()
    assert (sandbox / "tools" / "redact.py").exists()
    assert (sandbox / "harness" / "hooks" / "agentic_post_tool_global.py").exists()

    # Git initialized with the user-supplied remote
    assert (sandbox / ".git").is_dir()
    remote = subprocess.run(
        ["git", "-C", str(sandbox), "remote", "get-url", "origin"],
        capture_output=True, text=True,
    ).stdout.strip()
    # Note: a global insteadOf rule may rewrite ssh→https; check either form.
    assert (
        remote == fake_url
        or remote == "https://github.com/example/test-brain.git"
    ), f"remote URL not as expected: {remote}"

    # Initial commit was made
    log = subprocess.run(
        ["git", "-C", str(sandbox), "log", "--oneline"],
        capture_output=True, text=True,
    ).stdout
    assert "Initial brain" in log, f"no initial commit found: {log}"

    # Pre-commit hook installed
    hook = sandbox / ".git" / "hooks" / "pre-commit"
    assert hook.exists()
    assert hook.stat().st_mode & 0o100  # executable bit


@pytest.mark.skipif(_find_py310() is None, reason="needs Python >= 3.10 on PATH")
def test_brain_remote_via_env_var(tmp_path):
    """BRAIN_REMOTE_URL env var should work as an alternative to --brain-remote."""
    py = _find_py310()
    sandbox = tmp_path / ".agent"
    fake_url = "git@github.com:example/via-env.git"

    env = os.environ.copy()
    env["PYTHON_BIN"] = py
    env["BRAIN_REMOTE_URL"] = fake_url

    r = subprocess.run(
        [str(INSTALL_SH), "--brain-root", str(sandbox)],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert r.returncode == 0, f"install failed: stderr={r.stderr}"
    assert (sandbox / ".git").is_dir(), "BRAIN_REMOTE_URL should have triggered git init"


@pytest.mark.skipif(_find_py310() is None, reason="needs Python >= 3.10 on PATH")
def test_install_without_brain_remote_skips_git(tmp_path):
    """If --brain-remote is not given, the brain is NOT auto-initialized as a
    git repo (preserves the original opt-in behavior)."""
    py = _find_py310()
    sandbox = tmp_path / ".agent"

    env = os.environ.copy()
    env["PYTHON_BIN"] = py
    # Make sure BRAIN_REMOTE_URL isn't bleeding in from the parent shell
    env.pop("BRAIN_REMOTE_URL", None)

    r = subprocess.run(
        [str(INSTALL_SH), "--brain-root", str(sandbox)],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert r.returncode == 0
    assert (sandbox / "memory").is_dir()
    assert not (sandbox / ".git").exists(), (
        "without --brain-remote, install.sh must NOT git-init"
    )
