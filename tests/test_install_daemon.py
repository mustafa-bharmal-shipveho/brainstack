"""`install.sh` daemon flags: --setup-daemon / --remove-daemon / --no-daemon.

The warm daemon only earns its keep if it is actually running, which means a
launchd unit the installer renders, loads, and can tear down again. The
failure this file exists to prevent is the one the claude-extras plist hit in
2026-05: a template placeholder that never got substituted, producing a plist
launchd silently refuses, so auto-recall quietly falls back to the slow
in-process path forever.

Everything here is hermetic:
  * `HOME` is a tmp dir, so `~/Library/LaunchAgents` is a tmp dir;
  * `BRAINSTACK_SKIP_LAUNCHCTL=1` means no real `launchctl` call;
  * `BRAINSTACK_SKIP_CLI_INSTALL=1` skips the pip/venv leg.

Subprocess-level against the real `install.sh`, same harness as
`test_install_dry_run.py` and `test_uninstall_inventory.py`.
"""

from __future__ import annotations

import hashlib
import os
import platform
import plistlib
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"

PLIST_NAME = "com.brainstack.recall-daemon.plist"


def _summary_line(combined: str, opt_out_flag: str) -> str | None:
    """Find the install summary line mentioning an opt-out flag.

    Same shape the other install tests assert on:
      ✓ <description> (--no-X)     — the default fired
      • Skipped <thing> (--no-X)   — --no-X was passed
    """
    pattern = re.compile(rf"^\s*[✓•]\s.+\({re.escape(opt_out_flag)}\)\s*$", re.M)
    match = pattern.search(combined)
    return match.group(0) if match else None


def _fresh_env(fake_home: Path) -> dict:
    fake_home.mkdir(parents=True, exist_ok=True)
    (fake_home / "Library" / "LaunchAgents").mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["BRAIN_ROOT"] = str(fake_home / ".agent")
    env["BRAINSTACK_SKIP_LAUNCHCTL"] = "1"
    env["BRAINSTACK_SKIP_CLI_INSTALL"] = "1"
    env["GIT_AUTHOR_NAME"] = "DaemonTest"
    env["GIT_AUTHOR_EMAIL"] = "daemon@test"
    env["GIT_COMMITTER_NAME"] = "DaemonTest"
    env["GIT_COMMITTER_EMAIL"] = "daemon@test"
    return env


def _run(*args: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(INSTALL_SH), *args],
        env=env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
        timeout=180,
    )


def _seed_brain_root(env: dict) -> Path:
    """--setup-daemon refuses to run before a brain exists; make one."""
    brain = Path(env["BRAIN_ROOT"])
    (brain / "runtime" / "logs").mkdir(parents=True, exist_ok=True)
    (brain / "memory").mkdir(parents=True, exist_ok=True)
    return brain


def _tree_snapshot(root: Path) -> dict[str, str]:
    snap: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_symlink():
            snap[rel] = f"link:{os.readlink(p)}"
        elif p.is_dir():
            snap[rel] = "dir"
        elif p.is_file():
            snap[rel] = "file:" + hashlib.sha256(p.read_bytes()).hexdigest()
    return snap


darwin_only = pytest.mark.skipif(
    platform.system() != "Darwin",
    reason="the recall daemon unit is launchd-only today",
)


def test_help_lists_daemon_flags(tmp_path: Path):
    """A flag nobody can discover does not exist. `--help` is the only place
    the installer documents its modes."""
    env = _fresh_env(tmp_path / "fakehome")

    result = _run("--help", env=env)

    assert result.returncode == 0, f"--help failed:\n{result.stderr}"
    combined = result.stdout + result.stderr
    for flag in ("--setup-daemon", "--remove-daemon", "--no-daemon"):
        assert flag in combined, f"--help never mentions {flag}:\n{combined}"


def test_setup_daemon_dry_run_touches_nothing(tmp_path: Path):
    """--dry-run is a global plan mode. Combined with --setup-daemon it must
    print the plan and leave the filesystem byte-identical."""
    fake_home = tmp_path / "fakehome"
    env = _fresh_env(fake_home)
    _seed_brain_root(env)
    before = _tree_snapshot(fake_home)

    result = _run("--setup-daemon", "--dry-run", env=env)

    assert result.returncode == 0, (
        f"--setup-daemon --dry-run must succeed:\n{result.stdout}\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "DRY RUN" in combined, combined
    assert PLIST_NAME in combined, (
        f"the plan must name the plist it would write:\n{combined}"
    )
    after = _tree_snapshot(fake_home)
    assert after == before, (
        "--dry-run modified the filesystem: "
        f"added={set(after) - set(before)} removed={set(before) - set(after)} "
        f"changed={[k for k in set(after) & set(before) if after[k] != before[k]]}"
    )
    assert not (fake_home / "Library" / "LaunchAgents" / PLIST_NAME).exists()


@darwin_only
def test_setup_daemon_renders_plist_with_placeholders_replaced(tmp_path: Path):
    """Every `__PLACEHOLDER__` is substituted and the result parses as a plist.

    An unsubstituted placeholder produces a unit launchd rejects at load, and
    because the loader failure is silent the user just never gets a daemon.
    """
    fake_home = tmp_path / "fakehome"
    env = _fresh_env(fake_home)
    brain = _seed_brain_root(env)

    result = _run("--setup-daemon", env=env)

    assert result.returncode == 0, (
        f"--setup-daemon failed:\n{result.stdout}\n{result.stderr}"
    )
    plist_path = fake_home / "Library" / "LaunchAgents" / PLIST_NAME
    assert plist_path.exists(), (
        f"--setup-daemon wrote no plist at {plist_path}:\n{result.stdout}"
    )

    text = plist_path.read_text(encoding="utf-8")
    assert "__" not in text, (
        f"unsubstituted placeholder(s) left in the rendered plist:\n{text}"
    )

    with plist_path.open("rb") as fh:
        parsed = plistlib.load(fh)

    assert parsed["Label"] == "com.brainstack.recall-daemon"
    args = parsed["ProgramArguments"]
    assert Path(args[0]).is_absolute(), f"launchd ignores PATH; got {args[0]!r}"
    assert "recall.cli" in args, args
    assert "serve" in args, args

    envvars = parsed["EnvironmentVariables"]
    assert envvars["BRAIN_ROOT"] == str(brain)
    assert envvars["HOME"] == str(fake_home)
    assert Path(envvars["PYTHONPATH"]).exists(), envvars["PYTHONPATH"]

    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"] is True
    for stream in ("StandardOutPath", "StandardErrorPath"):
        assert str(brain) in parsed[stream], parsed[stream]


@darwin_only
def test_remove_daemon_deletes_plist(tmp_path: Path):
    """Teardown is reversible: --remove-daemon leaves nothing behind."""
    fake_home = tmp_path / "fakehome"
    env = _fresh_env(fake_home)
    _seed_brain_root(env)
    plist_path = fake_home / "Library" / "LaunchAgents" / PLIST_NAME
    plist_path.write_text(
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<plist version='1.0'><dict></dict></plist>",
        encoding="utf-8",
    )

    result = _run("--remove-daemon", env=env)

    assert result.returncode == 0, (
        f"--remove-daemon failed:\n{result.stdout}\n{result.stderr}"
    )
    assert not plist_path.exists(), (
        f"--remove-daemon left the plist behind:\n{result.stdout}"
    )


def test_remove_daemon_is_idempotent(tmp_path: Path):
    """Removing a daemon that was never installed is a no-op, not an error.
    `uninstall.sh` and re-runs both depend on that."""
    fake_home = tmp_path / "fakehome"
    env = _fresh_env(fake_home)
    _seed_brain_root(env)

    result = _run("--remove-daemon", env=env)

    assert result.returncode == 0, (
        f"--remove-daemon on a clean HOME must succeed:\n"
        f"{result.stdout}\n{result.stderr}"
    )


def test_no_daemon_flag_parses(tmp_path: Path):
    """`--no-daemon` opts a fresh install out of the daemon, like the other
    `--no-X` defaults. It must parse, not trip the unknown-argument guard."""
    fake_home = tmp_path / "fakehome"
    env = _fresh_env(fake_home)

    result = _run("--no-daemon", "--dry-run", env=env)

    combined = result.stdout + result.stderr
    assert "unknown argument" not in combined, combined
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


class TestDefaultInstallWiresTheDaemon:
    """The daemon has to be ON by default, or it never gets adopted.

    `--setup-daemon` working in isolation proves nothing about the path
    users actually take. A fresh `./install.sh --yes` is that path, and it is
    where a Default-6 block that was never wired in, or wired in after the
    early `exit 0`, shows up. Without the daemon every prompt silently pays
    the slow in-process retrieval path, which is the exact failure the warm
    daemon exists to remove.
    """

    @darwin_only
    def test_fresh_full_install_writes_daemon_plist(self, tmp_path: Path):
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)

        result = _run(
            "--brain-remote", "git@example.com:test/scratch.git",
            "--yes",
            env=env,
        )

        assert result.returncode == 0, (
            f"fresh full install failed (rc={result.returncode}):\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        plist_path = fake_home / "Library" / "LaunchAgents" / PLIST_NAME
        assert plist_path.exists(), (
            f"a default install did not install the daemon at {plist_path}; "
            f"auto-recall would run on the slow in-process path forever:\n"
            f"{result.stdout}"
        )

        combined = result.stdout + result.stderr
        line = _summary_line(combined, "--no-daemon")
        assert line is not None, (
            f"the install summary never mentions the daemon or its opt-out "
            f"flag:\n{combined}"
        )
        assert line.lstrip().startswith("✓"), (
            f"the daemon default fired, so its summary line should be ✓: "
            f"{line!r}"
        )

        # Adding a sixth default must not have broken the existing five.
        for other in ("--no-auto-migrate", "--no-launchd", "--no-auto-recall"):
            assert _summary_line(combined, other) is not None, (
                f"summary lost the line for {other}:\n{combined}"
            )

    @darwin_only
    def test_full_install_with_no_daemon_writes_no_plist(self, tmp_path: Path):
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)

        result = _run(
            "--brain-remote", "git@example.com:test/scratch.git",
            "--yes",
            "--no-daemon",
            env=env,
        )

        assert result.returncode == 0, (
            f"install with --no-daemon failed:\n{result.stdout}\n{result.stderr}"
        )
        plist_path = fake_home / "Library" / "LaunchAgents" / PLIST_NAME
        assert not plist_path.exists(), (
            f"--no-daemon still installed the daemon at {plist_path}; an "
            f"opt-out that does not opt out is worse than no flag:\n"
            f"{result.stdout}"
        )

        combined = result.stdout + result.stderr
        line = _summary_line(combined, "--no-daemon")
        assert line is not None, (
            f"summary missing the --no-daemon line:\n{combined}"
        )
        assert line.lstrip().startswith("•"), (
            f"with --no-daemon the summary line should be • (skipped): {line!r}"
        )

    def test_minimal_install_never_writes_daemon_plist(self, tmp_path: Path):
        """`--minimal` is the trust-building entry point: brain plus CLI, no
        host config, no scheduler. A LaunchAgent would break that promise."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)

        result = _run("--minimal", env=env)

        assert result.returncode == 0, (
            f"--minimal install failed:\n{result.stdout}\n{result.stderr}"
        )
        plist_dir = fake_home / "Library" / "LaunchAgents"
        plists = sorted(p.name for p in plist_dir.glob("*.plist"))
        assert PLIST_NAME not in plists, (
            f"--minimal installed the daemon LaunchAgent; it must touch no "
            f"scheduler at all. Found: {plists}"
        )


@pytest.mark.skipif(
    platform.system() == "Darwin",
    reason="covers the non-Darwin branch only",
)
def test_setup_daemon_on_non_darwin_explains_and_exits_zero(tmp_path: Path):
    """No systemd unit ships for the daemon yet. Linux users get told to run
    `recall serve` under their own supervisor rather than a silent failure."""
    fake_home = tmp_path / "fakehome"
    env = _fresh_env(fake_home)
    _seed_brain_root(env)

    result = _run("--setup-daemon", env=env)

    assert result.returncode == 0, (
        f"non-Darwin --setup-daemon must exit 0, not fail:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    combined = (result.stdout + result.stderr).lower()
    assert "launchd" in combined, combined
    assert "recall serve" in combined, combined
