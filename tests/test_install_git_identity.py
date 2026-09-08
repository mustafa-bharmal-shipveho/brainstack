"""`install.sh --brain-remote` on a machine with no git identity.

A fresh machine (or a CI runner, or an install run under a service account)
often has no `user.name` / `user.email` anywhere. The seed commit then dies
with "Author identity unknown" AFTER the brain directory was laid out and
BEFORE the LaunchAgents, hooks and daemon were installed — a half-install
with exit 128 (found by the 2026-09-08 full-day QA in an empty sandbox HOME).

The installer must give the brain repo a local identity when none is
configured, so the seed commit and every hourly `sync.sh` commit after it
succeed; and it must leave a configured identity alone.
"""

from __future__ import annotations

import os
import shutil
import subprocess
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


def _no_identity_env(tmp_path: Path) -> dict[str, str]:
    """An environment where git can find NO identity: empty HOME, no system
    or global config, no GIT_AUTHOR_* / GIT_COMMITTER_* variables."""
    home = tmp_path / "home"
    home.mkdir()
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))
    }
    env.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(home / "no-such-gitconfig"),
        "GIT_TERMINAL_PROMPT": "0",
        "PYTHON_BIN": _find_py310() or "python3",
        "BRAINSTACK_SKIP_CLI_INSTALL": "1",
        "BRAINSTACK_SKIP_LAUNCHCTL": "1",
    })
    return env


def _git(brain: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(brain), *args], capture_output=True, text=True,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"},
    ).stdout.strip()


@pytest.mark.skipif(_find_py310() is None, reason="needs Python >= 3.10 on PATH")
def test_install_without_any_git_identity_still_seeds_the_brain(tmp_path):
    env = _no_identity_env(tmp_path)
    brain = tmp_path / "home" / ".agent"
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)

    r = subprocess.run(
        [str(INSTALL_SH), "--yes", "--brain-root", str(brain),
         "--brain-remote", f"file://{remote}"],
        capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=180,
    )

    assert r.returncode == 0, f"rc={r.returncode}\nstdout:\n{r.stdout[-2000:]}\nstderr:\n{r.stderr[-2000:]}"
    assert "Author identity unknown" not in r.stdout + r.stderr
    assert "Initial brain" in _git(brain, "log", "--oneline"), _git(brain, "log", "--oneline")
    # The identity is REPO-LOCAL (the brain's .git/config), where sync.sh's
    # hourly commits — run by launchd with a minimal environment — find it.
    assert _git(brain, "config", "--local", "user.email") == "brainstack@localhost"
    assert _git(brain, "config", "--local", "user.name") == "brainstack"
    assert _git(brain, "log", "-1", "--format=%ae") == "brainstack@localhost"
    # And the installer said so, in words a first-time user can act on.
    assert "git identity" in r.stdout.lower(), r.stdout[-1500:]


@pytest.mark.skipif(_find_py310() is None, reason="needs Python >= 3.10 on PATH")
def test_install_leaves_a_configured_git_identity_alone(tmp_path):
    env = _no_identity_env(tmp_path)
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "Real Person"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "real@example.test"
    brain = tmp_path / "home" / ".agent"
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)

    r = subprocess.run(
        [str(INSTALL_SH), "--yes", "--brain-root", str(brain),
         "--brain-remote", f"file://{remote}"],
        capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=180,
    )

    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    assert _git(brain, "log", "-1", "--format=%ae") == "real@example.test"
    assert _git(brain, "config", "--local", "user.email") == ""
