"""TDD-red: `install.sh --minimal` - brain + recall CLI, nothing else.

The adoption audit found the default install too invasive for cautious
first-time users: it edits ~/.claude/settings.json, three host config
files, and installs LaunchAgents. --minimal is the trust-building entry
point: create the brain, install the recall CLI, print the commands to
enable everything else later, and touch NO host configs.

Subprocess-level integration tests against the real install.sh, isolated
via tmp HOME + BRAINSTACK_SKIP_LAUNCHCTL=1 + BRAINSTACK_SKIP_CLI_INSTALL=1
(same harness as test_install_hardening.py).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"


def _fresh_env(fake_home: Path) -> dict:
    fake_home.mkdir(parents=True, exist_ok=True)
    (fake_home / "Library" / "LaunchAgents").mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["BRAIN_ROOT"] = str(fake_home / ".agent")
    env["BRAINSTACK_SKIP_LAUNCHCTL"] = "1"
    env["BRAINSTACK_SKIP_CLI_INSTALL"] = "1"
    env["GIT_AUTHOR_NAME"] = "Minimal"
    env["GIT_AUTHOR_EMAIL"] = "minimal@test"
    env["GIT_COMMITTER_NAME"] = "Minimal"
    env["GIT_COMMITTER_EMAIL"] = "minimal@test"
    return env


def _run(*args: str, env: dict, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(INSTALL_SH), *args],
        env=env,
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
        timeout=180,
    )


def _seed_empty_host_files(fake_home: Path) -> dict[str, Path]:
    """Pre-create the three host config files EMPTY so we can assert
    --minimal leaves them byte-for-byte untouched."""
    (fake_home / ".claude").mkdir(parents=True, exist_ok=True)
    (fake_home / ".codex").mkdir(parents=True, exist_ok=True)
    (fake_home / ".cursor").mkdir(parents=True, exist_ok=True)
    files = {
        "claude_md": fake_home / ".claude" / "CLAUDE.md",
        "codex_md": fake_home / ".codex" / "AGENTS.md",
        "cursorrules": fake_home / ".cursor" / ".cursorrules",
    }
    for p in files.values():
        p.write_text("")
    return files


class TestMinimalInstall:
    def test_minimal_creates_brain_and_cli_only(self, tmp_path: Path):
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)
        host_files = _seed_empty_host_files(fake_home)

        result = _run("--minimal", env=env)
        assert result.returncode == 0, (
            f"--minimal install failed:\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

        # Brain root exists with the expected layout
        brain = fake_home / ".agent"
        assert brain.is_dir(), f"--minimal did not create the brain at {brain}"
        assert (brain / "memory").is_dir(), "brain missing memory/ dir"
        assert (brain / "tools").is_dir(), "brain missing tools/ dir"

        # NO host-config side effects
        assert not (fake_home / ".claude" / "settings.json").exists(), (
            "--minimal wrote ~/.claude/settings.json"
        )
        plists = list((fake_home / "Library" / "LaunchAgents").glob("*.plist"))
        assert plists == [], f"--minimal installed LaunchAgents: {plists}"

        # The three host files were pre-created empty and must stay empty -
        # no sentinel blocks, no edits of any kind
        for name, path in host_files.items():
            content = path.read_text()
            assert "brainstack-recall-first" not in content, (
                f"--minimal wrote a sentinel block into {name} ({path})"
            )
            assert content == "", (
                f"--minimal modified {name} ({path}); content now:\n{content}"
            )

    def test_minimal_prints_enable_later_commands(self, tmp_path: Path):
        """--minimal must teach the user how to opt in to each surface
        later, plus how to verify and how to leave."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)

        result = _run("--minimal", env=env)
        assert result.returncode == 0, (
            f"--minimal failed:\n{result.stdout}\n{result.stderr}"
        )

        out = result.stdout
        for snippet in (
            "--setup-launchd",
            "--setup-auto-migrate",
            "--setup-recall-first-all",
            "--enable-auto-recall",
            "--migrate",
            "recall doctor",
            "./uninstall.sh",
        ):
            assert snippet in out, (
                f"--minimal output missing enable-later command {snippet!r}:\n{out}"
            )

    def test_minimal_idempotent_second_run_status_only(self, tmp_path: Path):
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)

        r1 = _run("--minimal", env=env)
        assert r1.returncode == 0, (
            f"first --minimal failed:\n{r1.stdout}\n{r1.stderr}"
        )
        first_listing = sorted(
            str(p.relative_to(fake_home)) for p in fake_home.rglob("*")
        )

        r2 = _run("--minimal", env=env)
        assert r2.returncode == 0, (
            f"second --minimal failed:\n{r2.stdout}\n{r2.stderr}"
        )
        second_listing = sorted(
            str(p.relative_to(fake_home)) for p in fake_home.rglob("*")
        )
        assert second_listing == first_listing, (
            "second --minimal run duplicated or removed files.\n"
            f"added: {sorted(set(second_listing) - set(first_listing))}\n"
            f"removed: {sorted(set(first_listing) - set(second_listing))}"
        )

    def test_minimal_respects_brain_remote(self, tmp_path: Path):
        """--minimal --brain-remote: brain is a git repo with that origin."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)

        bare = tmp_path / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", str(bare)],
            capture_output=True, text=True, check=True,
        )

        result = _run("--minimal", "--brain-remote", str(bare), env=env)
        assert result.returncode == 0, (
            f"--minimal --brain-remote failed:\n{result.stdout}\n{result.stderr}"
        )

        brain = fake_home / ".agent"
        assert (brain / ".git").is_dir(), (
            "--minimal --brain-remote did not git-init the brain"
        )
        origin = subprocess.run(
            ["git", "-C", str(brain), "remote", "get-url", "origin"],
            capture_output=True, text=True, check=False,
        )
        assert origin.returncode == 0, (
            f"brain repo has no origin remote:\n{origin.stderr}"
        )
        assert origin.stdout.strip() == str(bare), (
            f"brain origin is {origin.stdout.strip()!r}, expected {str(bare)!r}"
        )
