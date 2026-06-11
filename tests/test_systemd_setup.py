"""TDD-red: Linux scheduler support via systemd user units.

The adoption audit found brainstack is macOS-only for scheduling: launchd
plists are the sole automation path, so Linux users silently get no sync,
no dream, no auto-migrate. The fix:

  - templates/systemd/ ships six user units (sync/dream/auto-migrate,
    each a .service + .timer pair).
  - `install.sh --setup-systemd` expands them into
    ~/.config/systemd/user/ (BRAINSTACK_SKIP_SYSTEMCTL=1 skips the
    `systemctl --user` calls for hermetic tests, mirroring
    BRAINSTACK_SKIP_LAUNCHCTL).
  - `install.sh --remove-systemd` deletes them.
  - The default install picks the scheduler by platform
    (BRAINSTACK_PLATFORM_OVERRIDE forces it for tests).
  - Unsupported platforms get a LOUD no-scheduler warning naming the
    cron/systemd alternatives.

Subprocess-level integration tests against the real install.sh, isolated
via tmp HOME (same harness as test_install_hardening.py).
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"
SYSTEMD_TEMPLATE_DIR = REPO_ROOT / "templates" / "systemd"

UNIT_NAMES = [
    "brainstack-sync.service",
    "brainstack-sync.timer",
    "brainstack-dream.service",
    "brainstack-dream.timer",
    "brainstack-auto-migrate.service",
    "brainstack-auto-migrate.timer",
]


def _fresh_env(fake_home: Path) -> dict:
    fake_home.mkdir(parents=True, exist_ok=True)
    (fake_home / "Library" / "LaunchAgents").mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["BRAIN_ROOT"] = str(fake_home / ".agent")
    env["BRAINSTACK_SKIP_LAUNCHCTL"] = "1"
    env["BRAINSTACK_SKIP_CLI_INSTALL"] = "1"
    env["BRAINSTACK_SKIP_SYSTEMCTL"] = "1"
    env["GIT_AUTHOR_NAME"] = "Systemd"
    env["GIT_AUTHOR_EMAIL"] = "systemd@test"
    env["GIT_COMMITTER_NAME"] = "Systemd"
    env["GIT_COMMITTER_EMAIL"] = "systemd@test"
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


def _unit_dir(fake_home: Path) -> Path:
    return fake_home / ".config" / "systemd" / "user"


def _stub_systemctl(tmp_path: Path, env: dict) -> Path:
    """Put a stub `systemctl` on PATH so platform detection sees one."""
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir(exist_ok=True)
    systemctl = stub_bin / "systemctl"
    systemctl.write_text("#!/bin/sh\nexit 0\n")
    systemctl.chmod(0o755)
    env["PATH"] = f"{stub_bin}{os.pathsep}{env['PATH']}"
    return systemctl


class TestSystemdSetup:
    def test_systemd_templates_exist_and_expand(self, tmp_path: Path):
        # Templates ship with the repo
        for name in UNIT_NAMES:
            assert (SYSTEMD_TEMPLATE_DIR / name).is_file(), (
                f"missing systemd template {SYSTEMD_TEMPLATE_DIR / name}"
            )

        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)

        result = _run("--setup-systemd", env=env)
        assert result.returncode == 0, (
            f"--setup-systemd failed:\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

        unit_dir = _unit_dir(fake_home)
        for name in UNIT_NAMES:
            unit = unit_dir / name
            assert unit.is_file(), (
                f"--setup-systemd did not write {unit}.\n"
                f"stdout:\n{result.stdout}"
            )
            content = unit.read_text()
            assert "REPLACE_" not in content, (
                f"{name} contains unexpanded REPLACE_ placeholders:\n{content}"
            )
            if name.endswith(".service"):
                assert "[Unit]" in content, f"{name} missing [Unit] section"
                assert "[Service]" in content, f"{name} missing [Service] section"
                # The unit must point at the brain root (default ~/.agent under
                # the fake home), not a path outside it. Guards the Codex fix
                # that threads BRAIN_ROOT through the template expansion.
                assert str(fake_home / ".agent") in content, (
                    f"{name} does not reference the brain root "
                    f"{fake_home / '.agent'}:\n{content}"
                )
            else:
                assert "[Timer]" in content, f"{name} missing [Timer] section"

    def test_remove_systemd_deletes_units(self, tmp_path: Path):
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)

        r1 = _run("--setup-systemd", env=env)
        assert r1.returncode == 0, (
            f"--setup-systemd failed:\n{r1.stdout}\n{r1.stderr}"
        )
        unit_dir = _unit_dir(fake_home)
        written = [n for n in UNIT_NAMES if (unit_dir / n).is_file()]
        assert written, (
            f"setup wrote no units to {unit_dir}; remove test is vacuous"
        )

        r2 = _run("--remove-systemd", env=env)
        assert r2.returncode == 0, (
            f"--remove-systemd failed:\n{r2.stdout}\n{r2.stderr}"
        )
        leftovers = [n for n in UNIT_NAMES if (unit_dir / n).exists()]
        assert leftovers == [], (
            f"--remove-systemd left units behind: {leftovers}"
        )


class TestPlatformSchedulerSelection:
    def test_linux_default_install_selects_systemd(self, tmp_path: Path):
        """On Linux (forced via BRAINSTACK_PLATFORM_OVERRIDE) with systemctl
        available, the full --yes install must set up systemd units, not
        launchd plists."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)
        env["BRAINSTACK_PLATFORM_OVERRIDE"] = "Linux"
        _stub_systemctl(tmp_path, env)

        result = _run("--yes", env=env)
        assert result.returncode == 0, (
            f"--yes install with Linux override failed:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        combined = result.stdout + result.stderr
        assert "systemd" in combined.lower(), (
            f"Linux install summary must mention systemd units:\n{combined}"
        )
        # No launchd plists on Linux
        plists = list((fake_home / "Library" / "LaunchAgents").glob("*.plist"))
        assert plists == [], (
            f"Linux install wrote launchd plists: {plists}"
        )
        # And the summary must not claim launchd was set up
        assert not re.search(r"✓[^\n]*launchd", combined, re.IGNORECASE), (
            f"Linux install summary claims launchd success:\n{combined}"
        )

    def test_unsupported_platform_prints_loud_no_scheduler_warning(
        self, tmp_path: Path
    ):
        """On a platform with neither launchd nor systemd, the install must
        complete but warn LOUDLY that no scheduler was installed, naming
        cron and systemd as the manual alternatives."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)
        env["BRAINSTACK_PLATFORM_OVERRIDE"] = "FreeBSD"

        result = _run("--yes", env=env)
        assert result.returncode == 0, (
            f"--yes install on unsupported platform must still succeed:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        combined = (result.stdout + result.stderr).lower()
        assert "scheduler" in combined, (
            f"no-scheduler warning missing on unsupported platform:\n"
            f"{result.stdout}\n{result.stderr}"
        )
        assert "cron" in combined, (
            f"no-scheduler warning must name cron as an alternative:\n"
            f"{result.stdout}\n{result.stderr}"
        )
        assert "systemd" in combined, (
            f"no-scheduler warning must name systemd as an alternative:\n"
            f"{result.stdout}\n{result.stderr}"
        )
        # Nothing scheduler-shaped was written
        plists = list((fake_home / "Library" / "LaunchAgents").glob("*.plist"))
        assert plists == [], f"unsupported platform wrote plists: {plists}"
        assert not _unit_dir(fake_home).exists() or not list(
            _unit_dir(fake_home).glob("brainstack-*")
        ), "unsupported platform wrote systemd units"
