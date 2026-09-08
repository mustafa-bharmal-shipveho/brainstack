"""BRAINSTACK_SKIP_LAUNCHCTL=1 must mean NO launchctl call, on every path.

launchd labels are per-USER, not per-HOME. A hermetic test or a tmp-HOME
smoke that runs `install.sh --uninstall` (what uninstall.sh delegates to)
with HOME pointed at a sandbox still runs `launchctl unload <sandbox plist>`
— and launchd resolves the plist's Label to the real user's job. The
2026-09-04 and 2026-09-08 full-day QA sandboxes each unloaded the live
nightly dream and hourly sync agents this way; the live brain then showed
"dream_cycle FAIL (87h ago)" and three days of unpushed commits.

The guard already existed for the setup-launchd and daemon paths. These
tests pin it for the uninstall and claude-extras paths too, with a fake
`launchctl` on PATH that records every invocation.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"
UNINSTALL_SH = REPO_ROOT / "uninstall.sh"


def _find_py310() -> str | None:
    for cand in ("python3.13", "python3.12", "python3.11", "python3.10"):
        path = shutil.which(cand)
        if path:
            return path
    return None


@pytest.fixture
def sandbox(tmp_path: Path):
    """A fake HOME with a brain, four rendered plists, and a recording
    `launchctl` first on PATH."""
    home = tmp_path / "home"
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    brain = home / ".agent"
    (brain / "runtime").mkdir(parents=True)
    # --setup-claude-extras refuses to render a plist for adapter scripts the
    # brain does not have; give it the real tools tree, as an install would.
    shutil.copytree(REPO_ROOT / "agent" / "tools", brain / "tools")
    for label in ("com.user.agent-dream", "com.user.agent-sync",
                  "com.brainstack.claude-extras", "com.brainstack.recall-daemon"):
        (agents / f"{label}.plist").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict><key>Label</key>'
            f'<string>{label}</string></dict></plist>\n'
        )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "launchctl-calls.log"
    fake = bin_dir / "launchctl"
    fake.write_text(f'#!/bin/sh\necho "launchctl $*" >> "{calls}"\nexit 0\n')
    fake.chmod(0o755)
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))
    }
    env.update({
        "HOME": str(home),
        "BRAIN_ROOT": str(brain),
        "PATH": f"{bin_dir}:{env.get('PATH', '')}",
        "PYTHON_BIN": _find_py310() or "python3",
        "BRAINSTACK_SKIP_LAUNCHCTL": "1",
        "BRAINSTACK_SKIP_CLI_INSTALL": "1",
    })
    return {"home": home, "brain": brain, "agents": agents, "env": env, "calls": calls}


def _calls(sb) -> str:
    return sb["calls"].read_text() if sb["calls"].exists() else ""


@pytest.mark.skipif(_find_py310() is None, reason="needs Python >= 3.10 on PATH")
def test_uninstall_never_calls_launchctl_under_the_skip_flag(sandbox):
    r = subprocess.run(
        [str(UNINSTALL_SH), "-y"], env=sandbox["env"], cwd=str(REPO_ROOT),
        capture_output=True, text=True, timeout=180,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    # The plist FILES in the sandbox HOME go away ...
    assert not list(sandbox["agents"].glob("*.plist")), list(sandbox["agents"].iterdir())
    # ... but launchd — which is the real user's — is never touched.
    assert _calls(sandbox) == "", f"launchctl was invoked:\n{_calls(sandbox)}"


@pytest.mark.skipif(_find_py310() is None, reason="needs Python >= 3.10 on PATH")
def test_remove_claude_extras_never_calls_launchctl_under_the_skip_flag(sandbox):
    r = subprocess.run(
        [str(INSTALL_SH), "--remove-claude-extras"], env=sandbox["env"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=180,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (sandbox["agents"] / "com.brainstack.claude-extras.plist").exists()
    assert _calls(sandbox) == "", f"launchctl was invoked:\n{_calls(sandbox)}"


@pytest.mark.skipif(_find_py310() is None, reason="needs Python >= 3.10 on PATH")
def test_setup_claude_extras_writes_the_plist_but_never_calls_launchctl(sandbox):
    (sandbox["agents"] / "com.brainstack.claude-extras.plist").unlink()
    r = subprocess.run(
        [str(INSTALL_SH), "--setup-claude-extras"], env=sandbox["env"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=180,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert (sandbox["agents"] / "com.brainstack.claude-extras.plist").exists()
    assert "skipped launchctl" in (r.stdout + r.stderr), r.stdout
    assert _calls(sandbox) == "", f"launchctl was invoked:\n{_calls(sandbox)}"
