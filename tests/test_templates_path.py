"""Every scheduled job must see `~/.local/bin` — S5 slice C, requirement R6.

launchd and systemd do not inherit a login shell's PATH. The shipped
plists set PATH to `/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:...`,
which omits `~/.local/bin` — where `claude`, `codex` and `recall` are
actually installed. Consequence on the live machine: the nightly dream
cycle resolved no LLM provider on every run and logged
`llm_errors=provider_unavailable=3` while the same command worked fine
from a terminal.

Fixing the provider lookup (tests/test_provider_fallback_paths.py) is
half of it. The other half is that the templates themselves must put
`$HOME/.local/bin` first, so anything the job shells out to resolves the
same binary the user does.

Covered here: the three launchd plists, the three systemd services, the
plist and unit that `auto_migrate_install` generates at runtime, and the
`--setup-claude-extras` render path in install.sh (whose template uses
`__HOME__`, a placeholder install.sh does not expand today).
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "templates"
INSTALL_SH = REPO_ROOT / "install.sh"

sys.path.insert(0, str(REPO_ROOT / "agent" / "tools"))
sys.path.insert(0, str(REPO_ROOT / "agent" / "memory"))

from auto_migrate_install import (  # noqa: E402
    generate_plist,
    generate_systemd_units,
)

LAUNCHD_TEMPLATES = (
    "com.user.agent-sync.plist",
    "com.user.agent-dream.plist",
    "com.brainstack.claude-extras.plist",
)

SYSTEMD_SERVICES = (
    "brainstack-sync.service",
    "brainstack-dream.service",
    "brainstack-auto-migrate.service",
)


def _substitute(text: str, *, home: str, brain: str, python: str) -> str:
    """Expand every placeholder the installer expands, so the result is
    what launchd / systemd actually reads."""
    for placeholder, value in (
        ("REPLACE_BRAIN_ROOT", brain),
        ("__BRAIN_ROOT__", brain),
        ("REPLACE_PYTHON", python),
        ("__PYTHON_ABS__", python),
        ("REPLACE_HOME", home),
        ("__HOME__", home),
    ):
        text = text.replace(placeholder, value)
    return text


# ---------------------------------------------------------------------------
# launchd plists
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template_name", LAUNCHD_TEMPLATES)
def test_launchd_templates_path_starts_with_home_local_bin(
    template_name: str, tmp_path: Path
):
    home = str(tmp_path / "home")
    brain = f"{home}/.agent"
    python = sys.executable

    raw = (TEMPLATES / template_name).read_text()
    text = _substitute(raw, home=home, brain=brain, python=python)
    assert "REPLACE_HOME" not in text and "__HOME__" not in text, (
        f"{template_name} still has an unexpanded home placeholder"
    )

    plist = plistlib.loads(text.encode())
    path = plist["EnvironmentVariables"]["PATH"]
    entries = path.split(":")

    assert path.startswith(f"{home}/.local/bin"), (
        f"{template_name} PATH must start with $HOME/.local/bin so the job "
        f"resolves the same `claude` / `codex` / `recall` the user does; "
        f"got {path!r}"
    )
    assert f"{home}/.claude/local" in entries, (
        f"{template_name} PATH must also include $HOME/.claude/local (the "
        f"Claude Code native-install dir); got {path!r}"
    )


# ---------------------------------------------------------------------------
# systemd units (Linux parity)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("service_name", SYSTEMD_SERVICES)
def test_systemd_services_declare_path_with_local_bin(
    service_name: str, tmp_path: Path
):
    home = str(tmp_path / "home")
    brain = f"{home}/.agent"

    raw = (TEMPLATES / "systemd" / service_name).read_text()
    assert "Environment=PATH=" in raw, (
        f"templates/systemd/{service_name} declares no PATH; systemd user "
        f"units start with a minimal environment, so `claude` / `codex` are "
        f"unreachable without one"
    )

    text = _substitute(raw, home=home, brain=brain, python=sys.executable)
    line = next(ln for ln in text.splitlines()
                if ln.startswith("Environment=PATH="))
    value = line.split("=", 2)[2]
    assert value.startswith(f"{home}/.local/bin"), (
        f"{service_name} PATH must start with $HOME/.local/bin; got {value!r}"
    )


# ---------------------------------------------------------------------------
# Units generated at runtime by auto_migrate_install
# ---------------------------------------------------------------------------


def test_auto_migrate_generate_plist_path_includes_local_bin(
    tmp_path: Path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    plist = plistlib.loads(
        generate_plist(tmp_path / "brain", Path(sys.executable))
    )
    path = plist["EnvironmentVariables"]["PATH"]
    assert path.startswith(f"{home}/.local/bin"), (
        f"the generated auto-migrate plist must lead with $HOME/.local/bin "
        f"(the adapters shell out to the same CLIs); got {path!r}"
    )
    assert f"{home}/.claude/local" in path.split(":"), path


def test_auto_migrate_generate_systemd_units_path_includes_local_bin(
    tmp_path: Path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    units = generate_systemd_units(tmp_path / "brain", Path(sys.executable))
    service = units["brainstack-auto-migrate.service"]
    lines = [ln for ln in service.splitlines()
             if ln.startswith("Environment=PATH=")]
    assert lines, (
        f"the generated systemd service declares no PATH:\n{service}"
    )
    value = lines[0].split("=", 2)[2]
    assert value.startswith(f"{home}/.local/bin"), value


# ---------------------------------------------------------------------------
# install.sh --setup-claude-extras must expand __HOME__
# ---------------------------------------------------------------------------


def test_install_sh_expands_home_placeholder_for_claude_extras(tmp_path: Path):
    """The claude-extras template uses `__HOME__` for its PATH, but the
    installer's sed only rewrites `__BRAIN_ROOT__` and `__PYTHON_ABS__`. An
    unexpanded placeholder would leave a literal `__HOME__/.local/bin` in
    the loaded plist, which is worse than the current wrong PATH."""
    install_text = INSTALL_SH.read_text()
    assert 's|__HOME__|$HOME|g' in install_text, (
        "install.sh --setup-claude-extras must add "
        '-e "s|__HOME__|$HOME|g" to its sed, or the rendered plist keeps '
        "the literal placeholder"
    )

    if sys.platform != "darwin":
        pytest.skip("--setup-claude-extras renders a launchd plist (macOS)")

    fake_home = tmp_path / "fakehome"
    (fake_home / "Library" / "LaunchAgents").mkdir(parents=True)
    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["BRAIN_ROOT"] = str(fake_home / ".agent")
    env["BRAINSTACK_SKIP_LAUNCHCTL"] = "1"
    env["BRAINSTACK_SKIP_CLI_INSTALL"] = "1"

    # A launchctl stub ahead of the real one: this test must never register
    # a job in the developer's session, whatever the installer decides to do
    # with BRAINSTACK_SKIP_LAUNCHCTL.
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    (stub_dir / "launchctl").write_text("#!/bin/sh\nexit 0\n")
    (stub_dir / "launchctl").chmod(0o755)
    env["PATH"] = f"{stub_dir}:{env.get('PATH', '')}"

    setup = subprocess.run(
        ["bash", str(INSTALL_SH), "--minimal"], env=env, cwd=str(REPO_ROOT),
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=180,
    )
    assert setup.returncode == 0, (
        f"--minimal failed:\nstdout:\n{setup.stdout}\nstderr:\n{setup.stderr}"
    )

    res = subprocess.run(
        ["bash", str(INSTALL_SH), "--setup-claude-extras"], env=env,
        cwd=str(REPO_ROOT), capture_output=True, text=True,
        stdin=subprocess.DEVNULL, timeout=180,
    )
    assert res.returncode == 0, (
        f"--setup-claude-extras failed:\nstdout:\n{res.stdout}\n"
        f"stderr:\n{res.stderr}"
    )

    plist_path = (fake_home / "Library" / "LaunchAgents"
                  / "com.brainstack.claude-extras.plist")
    assert plist_path.is_file(), "no plist was rendered"
    raw = plist_path.read_text()
    assert "__HOME__" not in raw, (
        f"the rendered plist still contains the literal placeholder:\n{raw}"
    )

    plist = plistlib.loads(plist_path.read_bytes())
    path = plist["EnvironmentVariables"]["PATH"]
    assert path.startswith(f"{fake_home}/.local/bin"), (
        f"rendered claude-extras PATH must lead with $HOME/.local/bin; "
        f"got {path!r}"
    )
