"""TDD-red: `install.sh --dry-run` as a true global plan mode.

Today --dry-run only works for `--migrate <path>` (bare `--dry-run` errors
with "requires a source path"). The adoption-audit fix turns it into a
first-class global mode: ANY invocation combined with --dry-run prints the
full plan of what would change and touches NOTHING on disk.

These are subprocess-level integration tests against the real install.sh,
isolated via tmp HOME + BRAINSTACK_SKIP_LAUNCHCTL=1 +
BRAINSTACK_SKIP_CLI_INSTALL=1 (same harness as test_install_hardening.py).
"""
from __future__ import annotations

import hashlib
import os
import platform
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
    env["GIT_AUTHOR_NAME"] = "DryRun"
    env["GIT_AUTHOR_EMAIL"] = "dryrun@test"
    env["GIT_COMMITTER_NAME"] = "DryRun"
    env["GIT_COMMITTER_EMAIL"] = "dryrun@test"
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


def _tree_snapshot(root: Path) -> dict[str, str]:
    """Recursive listing of root: relative path -> kind (+ content hash for
    files, link target for symlinks). Detects creations, deletions, AND
    in-place edits."""
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


class TestDefaultInstallDryRun:
    def test_default_install_dry_run_creates_nothing(self, tmp_path: Path):
        """`install.sh --dry-run` on a fresh HOME: rc 0, prints the plan,
        and the filesystem is byte-for-byte identical before and after."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)
        before = _tree_snapshot(fake_home)

        result = _run("--dry-run", env=env)
        assert result.returncode == 0, (
            f"bare --dry-run must succeed (it is the global plan mode now):\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        assert not (fake_home / ".agent").exists(), (
            "--dry-run created the brain root"
        )
        assert not (fake_home / ".claude" / "settings.json").exists(), (
            "--dry-run wrote ~/.claude/settings.json"
        )
        after = _tree_snapshot(fake_home)
        assert after == before, (
            "--dry-run changed the filesystem.\n"
            f"before: {sorted(before)}\nafter: {sorted(after)}"
        )

    def test_default_install_dry_run_prints_full_plan(self, tmp_path: Path):
        """The plan must name every surface the default install would touch:
        brain root, settings.json, the three host config files, launchd
        (on Darwin), and the recall CLI symlink."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)

        result = _run("--dry-run", env=env)
        assert result.returncode == 0, (
            f"--dry-run failed:\n{result.stdout}\n{result.stderr}"
        )

        out = result.stdout
        brain_root = str(fake_home / ".agent")
        assert brain_root in out, (
            f"plan does not name the brain root {brain_root}:\n{out}"
        )
        for marker in ("settings.json", "CLAUDE.md", "AGENTS.md", ".cursorrules"):
            assert marker in out, f"plan missing surface {marker!r}:\n{out}"
        if platform.system() == "Darwin":
            assert "LaunchAgents" in out, (
                f"plan on Darwin must mention LaunchAgents:\n{out}"
            )
        assert ".local/bin/recall" in out, (
            f"plan missing the recall CLI symlink path:\n{out}"
        )

    def test_dry_run_with_existing_brain_prints_status_only(self, tmp_path: Path):
        """--dry-run against an existing brain: status output, zero changes."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)
        brain = fake_home / ".agent"
        (brain / "memory").mkdir(parents=True)
        (brain / "tools").mkdir(parents=True)
        sentinel = brain / "memory" / "do-not-touch.md"
        sentinel.write_text("PRE-EXISTING USER CONTENT\n")
        before = _tree_snapshot(fake_home)

        result = _run("--dry-run", env=env)
        assert result.returncode == 0, (
            f"--dry-run on existing brain failed:\n{result.stdout}\n{result.stderr}"
        )

        combined = result.stdout + result.stderr
        assert "already exists" in combined or "status" in combined.lower(), (
            f"existing-brain --dry-run should report status:\n{combined}"
        )
        assert sentinel.read_text() == "PRE-EXISTING USER CONTENT\n"
        after = _tree_snapshot(fake_home)
        assert after == before, "--dry-run on existing brain changed the filesystem"

    def test_dry_run_install_scanner_does_not_install(self, tmp_path: Path):
        """--dry-run --install-scanner must NOT invoke the package manager.
        A stub `brew` on PATH writes a marker file if invoked; the marker
        must stay absent."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)

        marker = tmp_path / "brew-invoked.marker"
        stub_bin = tmp_path / "stub-bin"
        stub_bin.mkdir()
        brew = stub_bin / "brew"
        brew.write_text(
            "#!/bin/sh\n"
            f"echo \"$@\" >> '{marker}'\n"
            "exit 0\n"
        )
        brew.chmod(0o755)
        env["PATH"] = f"{stub_bin}{os.pathsep}{env['PATH']}"

        before = _tree_snapshot(fake_home)
        result = _run("--dry-run", "--install-scanner", env=env)
        assert result.returncode == 0, (
            f"--dry-run --install-scanner failed:\n{result.stdout}\n{result.stderr}"
        )
        assert not marker.exists(), (
            f"--dry-run invoked brew (marker written):\n{marker.read_text() if marker.exists() else ''}"
        )
        after = _tree_snapshot(fake_home)
        assert after == before, (
            "--dry-run --install-scanner changed the fake HOME"
        )


class TestSetupModeDryRun:
    """Every --setup-X mode combined with --dry-run must be a pure no-op
    on disk: plan printed, rc 0, file tree (including file CONTENT, since
    several modes edit host files in place) unchanged."""

    @pytest.mark.parametrize("mode", [
        "--setup-claude-extras",
        "--setup-shell-banner",
        "--setup-digests",
        "--setup-recall-first-all",
        "--enable-auto-recall",
        "--setup-launchd",
    ])
    def test_setup_mode_dry_run_no_filesystem_diff(self, tmp_path: Path, mode: str):
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)
        # Seed a realistic surface so each mode has something it WOULD touch
        brain = fake_home / ".agent"
        (brain / "memory").mkdir(parents=True)
        (brain / "tools").mkdir(parents=True)
        runtime = brain / "runtime"
        runtime.mkdir(parents=True)
        (runtime / "pyproject.toml").write_text(
            "[tool.brainstack]\nenable_auto_recall = false\n"
        )
        (fake_home / ".claude").mkdir()
        (fake_home / ".claude" / "CLAUDE.md").write_text("# user claude config\n")
        (fake_home / ".codex").mkdir()
        (fake_home / ".codex" / "AGENTS.md").write_text("# user codex config\n")
        (fake_home / ".cursor").mkdir()
        (fake_home / ".cursor" / ".cursorrules").write_text("# user cursor rules\n")
        (fake_home / ".zshrc").write_text("# user zshrc\n")

        before = _tree_snapshot(fake_home)
        result = _run(mode, "--dry-run", env=env)
        assert result.returncode == 0, (
            f"{mode} --dry-run failed:\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
        after = _tree_snapshot(fake_home)
        assert after == before, (
            f"{mode} --dry-run changed the filesystem.\n"
            f"added/changed: "
            f"{ {k: v for k, v in after.items() if before.get(k) != v} }\n"
            f"removed: {sorted(set(before) - set(after))}"
        )
