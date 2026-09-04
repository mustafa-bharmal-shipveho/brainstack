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
TEMPLATE_PATH = REPO_ROOT / "templates" / PLIST_NAME


def _summary_line(combined: str, opt_out_flag: str) -> str | None:
    """Find the install summary line mentioning an opt-out flag.

    Same shape the other install tests assert on:
      ✓ <description> (--no-X)     — the default fired
      • Skipped <thing> (--no-X)   — --no-X was passed
      ✗ <description> (--no-X)     — the default failed
    """
    pattern = re.compile(rf"^\s*[✓•✗]\s.+\({re.escape(opt_out_flag)}\)\s*$", re.M)
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
def test_setup_daemon_home_with_double_underscore_renders_fine(tmp_path: Path):
    """A HOME (or BRAIN_ROOT/REPO_DIR) that happens to contain a literal
    double underscore is not an unsubstituted placeholder. The old
    post-render check was a blanket `grep -q '__'`, which false-positives on
    any such path, deletes the plist it just wrote, and exits 2 -- so
    Default 6 silently reports the daemon as 'failed' for a path that was
    never a bug."""
    fake_home = tmp_path / "fake__home__dir"
    env = _fresh_env(fake_home)
    _seed_brain_root(env)

    result = _run("--setup-daemon", env=env)

    assert result.returncode == 0, (
        f"a HOME containing '__' must not be treated as an unsubstituted "
        f"placeholder:\n{result.stdout}\n{result.stderr}"
    )
    plist_path = fake_home / "Library" / "LaunchAgents" / PLIST_NAME
    assert plist_path.exists(), (
        f"--setup-daemon deleted the plist it rendered because HOME "
        f"contains '__':\n{result.stdout}\n{result.stderr}"
    )
    plist = plistlib.loads(plist_path.read_bytes())
    assert plist["EnvironmentVariables"]["HOME"] == str(fake_home), plist


@darwin_only
def test_setup_daemon_renders_plist_with_space_and_ampersand_in_paths(tmp_path: Path):
    """Rendering goes through Python now, not `sed -e "s|...|...|g"`: a
    replacement value containing `&` makes sed re-insert the whole match,
    and one containing `\\1` is read as a backreference. HOME and BRAIN_ROOT
    both get a space and an `&` to prove the injection surface is gone and
    the exact values still land in the rendered plist."""
    fake_home = tmp_path / "fake home & co"
    env = _fresh_env(fake_home)
    brain = fake_home / "brain & data"
    env["BRAIN_ROOT"] = str(brain)
    _seed_brain_root(env)

    result = _run("--setup-daemon", env=env)

    assert result.returncode == 0, (
        f"--setup-daemon must handle spaces and '&' in HOME/BRAIN_ROOT:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    plist_path = fake_home / "Library" / "LaunchAgents" / PLIST_NAME
    assert plist_path.exists(), f"no plist rendered:\n{result.stdout}"

    plist = plistlib.loads(plist_path.read_bytes())
    envvars = plist["EnvironmentVariables"]
    assert envvars["HOME"] == str(fake_home), envvars
    assert envvars["BRAIN_ROOT"] == str(brain), envvars
    for stream in ("StandardOutPath", "StandardErrorPath"):
        assert str(brain) in plist[stream], plist[stream]


@darwin_only
def test_daemon_template_path_starts_with_home_local_bin():
    """R6 consistency: launchd and systemd do not inherit a login shell's
    PATH, so every scheduled job must put `~/.local/bin` first or it cannot
    resolve the same `claude` / `codex` / `recall` the user gets from a
    terminal (see tests/test_templates_path.py). The daemon template was the
    one shipped plist that didn't."""
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    match = re.search(r"<key>PATH</key>\s*<string>([^<]+)</string>", text)
    assert match, f"no PATH entry found in {TEMPLATE_PATH}"
    path_value = match.group(1)
    assert path_value.startswith("__HOME__/.local/bin:__REPO_DIR__/.venv/bin:"), (
        f"{TEMPLATE_PATH} PATH must start with __HOME__/.local/bin, "
        f"immediately followed by __REPO_DIR__/.venv/bin (so the venv's own "
        f"qdrant_client/fastembed still win); got {path_value!r}"
    )


@darwin_only
def test_setup_daemon_rendered_path_leads_with_home_local_bin(tmp_path: Path):
    """End-to-end companion to the template-only check above: the fully
    substituted PATH in the rendered plist must lead with the real HOME."""
    fake_home = tmp_path / "fakehome"
    env = _fresh_env(fake_home)
    _seed_brain_root(env)

    result = _run("--setup-daemon", env=env)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    plist_path = fake_home / "Library" / "LaunchAgents" / PLIST_NAME
    plist = plistlib.loads(plist_path.read_bytes())
    path_value = plist["EnvironmentVariables"]["PATH"]
    assert path_value.startswith(f"{fake_home}/.local/bin:"), (
        f"rendered daemon PATH must lead with $HOME/.local/bin (R6); got "
        f"{path_value!r}"
    )


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
    def test_fresh_full_install_forwards_custom_brain_root_to_daemon(self, tmp_path: Path):
        """`--brain-root /custom` must reach the recursive `--setup-daemon`
        step. The parsed BRAIN_ROOT is a shell variable, not an exported
        one, so an unforwarded recursion would silently configure the
        LaunchAgent for the wrong brain. The env deliberately points
        BRAIN_ROOT at a different dir so the flag has to win."""
        import plistlib

        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)
        custom = tmp_path / "custom-brain"
        # A real user's shell has no BRAIN_ROOT at all; only the flag names
        # the brain. (With BRAIN_ROOT in the env, bash keeps the export
        # attribute when the flag reassigns it and the bug is masked.)
        env.pop("BRAIN_ROOT", None)

        result = _run(
            "--brain-remote", "git@example.com:test/scratch.git",
            "--brain-root", str(custom),
            "--yes",
            env=env,
        )
        assert result.returncode == 0, (
            f"fresh full install failed (rc={result.returncode}):\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        plist_path = fake_home / "Library" / "LaunchAgents" / PLIST_NAME
        assert plist_path.exists(), f"daemon plist missing:\n{result.stdout}"
        plist = plistlib.loads(plist_path.read_bytes())
        env_vars = plist.get("EnvironmentVariables", {})
        assert env_vars.get("BRAIN_ROOT") == str(custom), (
            f"daemon plist BRAIN_ROOT should be the --brain-root value "
            f"{str(custom)!r}, got {env_vars.get('BRAIN_ROOT')!r}"
        )
        assert str(fake_home / ".agent") not in plist_path.read_text()

    @darwin_only
    def test_daemon_failure_logs_stderr_and_summary_shows_last_line(self, tmp_path: Path):
        """Default 6 used to run `--setup-daemon >/dev/null 2>&1`, so on
        failure the summary just said 'failed' with no way to find out why.
        Force a deterministic --setup-daemon failure -- a directory sitting
        where the plist must go, so the render step can never write there --
        and check the stderr lands in a log the summary points at."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)
        blocked = fake_home / "Library" / "LaunchAgents" / PLIST_NAME
        blocked.mkdir(parents=True)

        result = _run(
            "--brain-remote", "git@example.com:test/scratch.git",
            "--yes",
            env=env,
        )

        assert result.returncode == 0, (
            f"a daemon failure must not fail the whole install:\n"
            f"{result.stdout}\n{result.stderr}"
        )
        combined = result.stdout + result.stderr
        line = _summary_line(combined, "--no-daemon")
        assert line is not None and line.lstrip().startswith("✗"), (
            f"expected a failed daemon summary line:\n{combined}"
        )

        log_path = Path(env["BRAIN_ROOT"]) / "runtime" / "logs" / "install-daemon.log"
        assert log_path.exists() and log_path.stat().st_size > 0, (
            f"Default 6 must capture --setup-daemon's stderr to "
            f"{log_path}:\n{combined}"
        )
        last_line = log_path.read_text().strip().splitlines()[-1]
        assert last_line, f"log at {log_path} has no content"
        assert last_line in combined, (
            f"the summary must print the log's last line after the failed "
            f"line:\nlog last line: {last_line!r}\ncombined:\n{combined}"
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
