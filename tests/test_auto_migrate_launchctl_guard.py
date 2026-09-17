"""BRAINSTACK_SKIP_LAUNCHCTL=1 must mean ZERO launchctl invocations from
auto_migrate_install's launchd paths — both `setup` and `remove`.

launchd labels are per-USER, not per-HOME. A hermetic test or tmp-HOME run
that still shells out to `launchctl bootout/bootstrap/kickstart` reaches
the real user's launchd domain (gui/<uid>) and can break the live
com.brainstack.auto-migrate job.

Unlike tests/test_install_launchctl_guard.py (a fake launchctl binary on
PATH), these tests use the in-process `launchctl_bin` callable seam that
`main(argv, launchctl_bin=...)` accepts — no subprocess, no PATH games.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from agent.tools.auto_migrate_install import LABEL, main

class FakeLaunchctl:
    """Recording fake for the `launchctl_bin` callable seam."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    @property
    def argv_lines(self) -> list[str]:
        return [" ".join(c) for c in self.calls]


def _setup_args(tmp_path: Path) -> list[str]:
    return [
        "setup",
        "--scheduler", "launchd",
        "--none",  # write config with no tools; no interactive prompting
        "--brain-root", str(tmp_path / "brain"),
        "--plist-dir", str(tmp_path / "plists"),
    ]


def _remove_args(tmp_path: Path) -> list[str]:
    return [
        "remove",
        "--scheduler", "launchd",
        "--brain-root", str(tmp_path / "brain"),
        "--plist-dir", str(tmp_path / "plists"),
    ]


def _uid() -> str:
    import os
    return str(os.geteuid())


def _seed_plist(tmp_path: Path) -> Path:
    plist_dir = tmp_path / "plists"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist = plist_dir / f"{LABEL}.plist"
    plist.write_bytes(b"<plist><dict/></plist>")
    return plist


# ---- setup ----


def test_setup_under_skip_flag_writes_plist_but_makes_zero_launchctl_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAINSTACK_SKIP_LAUNCHCTL", "1")
    fake = FakeLaunchctl()
    rc = main(
        _setup_args(tmp_path),
        launchctl_bin=fake,
    )

    assert rc == 0
    # Plist landed in --plist-dir...
    plist = tmp_path / "plists" / f"{LABEL}.plist"
    assert plist.exists(), "plist must still be written under SKIP=1"
    # ...and the config still lives under the brain root.
    assert (tmp_path / "brain" / "auto-migrate.json").exists()
    # But launchd was never touched.
    assert fake.calls == [], (
        f"launchctl was invoked under BRAINSTACK_SKIP_LAUNCHCTL=1:\n"
        + "\n".join(fake.argv_lines)
    )


def test_setup_without_skip_flag_fires_bootout_bootstrap_kickstart_in_order(tmp_path, monkeypatch):
    monkeypatch.delenv("BRAINSTACK_SKIP_LAUNCHCTL", raising=False)
    fake = FakeLaunchctl()
    rc = main(
        _setup_args(tmp_path),
        launchctl_bin=fake,
    )

    assert rc == 0
    uid = _uid()
    # The invoker receives argv with the binary name as argv[0]; strip it.
    verbs = [c[1] for c in fake.calls]
    assert verbs == ["bootout", "bootstrap", "kickstart"], (
        "expected bootout, bootstrap, kickstart in order, got:\n"
        + "\n".join(fake.argv_lines)
    )
    assert fake.calls[0][1:] == ["bootout", f"gui/{uid}/{LABEL}"]
    assert fake.calls[1][1:3] == ["bootstrap", f"gui/{uid}"]
    assert fake.calls[1][3].endswith(f"{LABEL}.plist")
    assert fake.calls[2][1:] == ["kickstart", "-k", f"gui/{uid}/{LABEL}"]


# ---- remove ----


def test_remove_under_skip_flag_deletes_plist_but_makes_zero_launchctl_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAINSTACK_SKIP_LAUNCHCTL", "1")
    plist = _seed_plist(tmp_path)
    fake = FakeLaunchctl()
    rc = main(
        _remove_args(tmp_path),
        launchctl_bin=fake,
    )

    assert rc == 0
    assert not plist.exists(), "plist must still be deleted under SKIP=1"
    assert fake.calls == [], (
        f"launchctl was invoked under BRAINSTACK_SKIP_LAUNCHCTL=1:\n"
        + "\n".join(fake.argv_lines)
    )


def test_remove_without_skip_flag_fires_bootout_and_deletes_plist(tmp_path, monkeypatch):
    monkeypatch.delenv("BRAINSTACK_SKIP_LAUNCHCTL", raising=False)
    plist = _seed_plist(tmp_path)
    fake = FakeLaunchctl()
    rc = main(
        _remove_args(tmp_path),
        launchctl_bin=fake,
    )

    assert rc == 0
    assert not plist.exists()
    assert fake.calls == [["launchctl", "bootout", f"gui/{_uid()}/{LABEL}"]], (
        "expected exactly one bootout against gui/<uid>/<label>, got:\n"
        + "\n".join(fake.argv_lines)
    )
