"""Uninstall dry-run preview must list every plist the live uninstall removes.

Repro: in the v0.6.0 live test (mustafa@2026-05-20), `./uninstall.sh --dry-run`
previewed 2 plists (`dream`, `sync`) but the actual uninstall removed 4
(`dream`, `sync`, `auto-migrate`, `claude-extras`). Two of those — auto-migrate
and claude-extras — use the `com.brainstack.*` prefix, not `com.user.agent.*`,
and the dry-run inventory list had the wrong names.

Fix: align the inventory plist-name list with what `--setup-X` actually writes.

This test seeds all 4 expected plists and runs `--dry-run`, then asserts
each one appears in the "Will REMOVE" preview. If a future contributor
adds a new --setup-X mode without updating the uninstall inventory, this
test will fail with the missing-plist name in the failure message.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"


def _fresh_env(fake_home: Path) -> dict:
    fake_home.mkdir(parents=True, exist_ok=True)
    (fake_home / "Library" / "LaunchAgents").mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["BRAIN_ROOT"] = str(fake_home / ".agent")
    env["BRAINSTACK_SKIP_LAUNCHCTL"] = "1"
    return env


def _run_uninstall(*args: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(INSTALL_SH), "--uninstall", *args],
        env=env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )


def test_dry_run_lists_all_four_plists_when_present(tmp_path: Path):
    """Seed all 4 plists the live install writes. --dry-run preview must
    list every one of them in 'Will REMOVE'."""
    fake_home = tmp_path / "fakehome"
    env = _fresh_env(fake_home)
    plist_dir = fake_home / "Library" / "LaunchAgents"

    expected = [
        "com.user.agent-dream.plist",
        "com.user.agent-sync.plist",
        "com.brainstack.auto-migrate.plist",
        "com.brainstack.claude-extras.plist",
    ]
    for name in expected:
        (plist_dir / name).write_text(
            "<?xml version='1.0' encoding='UTF-8'?>"
            "<plist version='1.0'><dict></dict></plist>"
        )

    result = _run_uninstall("--dry-run", env=env)
    assert result.returncode == 0, (
        f"--dry-run failed:\n{result.stdout}\n{result.stderr}"
    )

    combined = result.stdout + result.stderr
    for name in expected:
        assert name in combined, (
            f"--dry-run preview missing plist {name!r}:\n{combined}"
        )


def test_dry_run_only_lists_plists_that_exist(tmp_path: Path):
    """If a plist is NOT present, dry-run must NOT claim it'll be removed.
    Sanity check on the inventory's per-file existence guard."""
    fake_home = tmp_path / "fakehome"
    env = _fresh_env(fake_home)
    plist_dir = fake_home / "Library" / "LaunchAgents"

    # Only seed dream + sync; leave auto-migrate + claude-extras absent
    (plist_dir / "com.user.agent-dream.plist").write_text("<plist/>")
    (plist_dir / "com.user.agent-sync.plist").write_text("<plist/>")

    result = _run_uninstall("--dry-run", env=env)
    assert result.returncode == 0

    combined = result.stdout + result.stderr
    assert "com.user.agent-dream.plist" in combined
    assert "com.user.agent-sync.plist" in combined
    # Absent ones should NOT appear
    assert "com.brainstack.auto-migrate.plist" not in combined, (
        f"dry-run claimed to remove a plist that doesn't exist:\n{combined}"
    )
    assert "com.brainstack.claude-extras.plist" not in combined, (
        f"dry-run claimed to remove a plist that doesn't exist:\n{combined}"
    )


def test_dry_run_lists_systemd_units_when_present(tmp_path: Path):
    """Linux parity: seed the six systemd user units --setup-systemd writes.
    The --dry-run preview must list every one of them, same contract as the
    launchd plists."""
    fake_home = tmp_path / "fakehome"
    env = _fresh_env(fake_home)
    env["BRAINSTACK_SKIP_SYSTEMCTL"] = "1"
    unit_dir = fake_home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)

    expected = [
        "brainstack-sync.service",
        "brainstack-sync.timer",
        "brainstack-dream.service",
        "brainstack-dream.timer",
        "brainstack-auto-migrate.service",
        "brainstack-auto-migrate.timer",
    ]
    for name in expected:
        (unit_dir / name).write_text("[Unit]\nDescription=test seed\n")

    result = _run_uninstall("--dry-run", env=env)
    assert result.returncode == 0, (
        f"--dry-run failed:\n{result.stdout}\n{result.stderr}"
    )

    combined = result.stdout + result.stderr
    for name in expected:
        assert name in combined, (
            f"--dry-run preview missing systemd unit {name!r}:\n{combined}"
        )


def test_uninstall_removes_seeded_systemd_units(tmp_path: Path):
    """Live uninstall must delete the seeded systemd units (with
    BRAINSTACK_SKIP_SYSTEMCTL=1 so no real systemctl is needed)."""
    fake_home = tmp_path / "fakehome"
    env = _fresh_env(fake_home)
    env["BRAINSTACK_SKIP_SYSTEMCTL"] = "1"
    unit_dir = fake_home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    names = [
        "brainstack-sync.service",
        "brainstack-sync.timer",
        "brainstack-dream.service",
        "brainstack-dream.timer",
        "brainstack-auto-migrate.service",
        "brainstack-auto-migrate.timer",
    ]
    for name in names:
        (unit_dir / name).write_text("[Unit]\nDescription=test seed\n")

    result = _run_uninstall("-y", env=env)
    assert result.returncode == 0, (
        f"uninstall failed:\n{result.stdout}\n{result.stderr}"
    )
    leftovers = [n for n in names if (unit_dir / n).exists()]
    assert leftovers == [], f"uninstall left systemd units behind: {leftovers}"


def test_inventory_and_removal_use_same_systemd_unit_names():
    """Static guard, mirroring the plist-name check below: the uninstall
    inventory loop and the removal loop must list the SAME systemd unit
    names, and there must be all six of them."""
    content = (REPO_ROOT / "install.sh").read_text()

    inventory_match = re.search(
        r"# Inventory the systemd user units.*?(?=\n\s*for unit in)(.*?)(?=\s*done)",
        content, re.DOTALL,
    )
    removal_match = re.search(
        r"# Remove systemd user units\.(.*?)(?=\s*done)",
        content, re.DOTALL,
    )
    assert inventory_match, "couldn't locate systemd inventory loop in install.sh"
    assert removal_match, "couldn't locate systemd removal loop in install.sh"

    def extract_names(block: str) -> set[str]:
        return set(re.findall(r"\$systemd_unit_dir/([^\"]+\.(?:service|timer))", block))

    inv_names = extract_names(inventory_match.group(1))
    rm_names = extract_names(removal_match.group(1))

    assert inv_names == rm_names, (
        f"systemd inventory + removal unit lists differ.\n"
        f"  inventory only: {inv_names - rm_names}\n"
        f"  removal only:   {rm_names - inv_names}"
    )
    assert len(inv_names) == 6, (
        f"expected the six brainstack systemd units, got: {inv_names}"
    )


def test_inventory_and_removal_use_same_plist_names(tmp_path: Path):
    """Static guard: the dry-run inventory loop and the actual removal loop
    in install.sh must use the SAME plist names. Otherwise the preview
    could under-report (the original bug) or over-report."""
    content = (REPO_ROOT / "install.sh").read_text()

    # Find both loops by their distinguishing lines
    inventory_match = re.search(
        r"# Inventory the launchd plists.*?(?=\n\s*for plist in)(.*?)(?=\s*done)",
        content, re.DOTALL,
    )
    removal_match = re.search(
        r"# Unload \+ remove launchd plists\.(.*?)(?=\s*done)",
        content, re.DOTALL,
    )

    assert inventory_match, "couldn't locate inventory loop in install.sh"
    assert removal_match, "couldn't locate removal loop in install.sh"

    def extract_names(block: str) -> set[str]:
        return set(re.findall(r'\$plist_dir/([^"]+\.plist)', block))

    inv_names = extract_names(inventory_match.group(1))
    rm_names = extract_names(removal_match.group(1))

    assert inv_names == rm_names, (
        f"inventory + removal plist name lists differ — this is the original bug.\n"
        f"  inventory only: {inv_names - rm_names}\n"
        f"  removal only:   {rm_names - inv_names}"
    )
    assert len(inv_names) >= 4, (
        f"plist name list shrunk below 4 entries — expected at least dream, "
        f"sync, auto-migrate, claude-extras. Got: {inv_names}"
    )
