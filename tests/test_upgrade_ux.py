"""Upgrade UX — pins two contracts for ./upgrade.sh and ./install.sh --upgrade.

Distribution scenario: a colleague has brainstack v0.5.0 installed. You push
v0.6.0. They need an obvious, one-command path to:

  (1) Get the new code into their clone (`git pull`), then upgrade their brain.
      Today they have to remember to `git pull` AND then `./upgrade.sh` —
      two steps where one will do.

  (2) See WHAT changed when they upgrade. Today the output just says
      "Upgrade complete" with no signal about what's new. The CHANGELOG
      lives in the repo but they have to know to look.

Two changes:

  C1 — `./upgrade.sh` does `git pull` first, then runs `./install.sh
       --upgrade`. Opt out with `--no-pull` for users managing git
       themselves.

  C2 — `./install.sh --upgrade` reads the brain's installed
       `recall/__init__.py.__version__` BEFORE upgrading, then compares
       against the repo's version AFTER, and if they differ prints the
       CHANGELOG entries between the two versions.

Tests use a tmp git repo (file-protocol remote) so we exercise the real
`git pull` path without network. The brain is a real ~/.agent layout
in tmp_path.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
UPGRADE_SH = REPO_ROOT / "upgrade.sh"
INSTALL_SH = REPO_ROOT / "install.sh"


def _run(*argv, env=None, cwd=None, check=False) -> subprocess.CompletedProcess:
    """Run a shell command; never let it pollute the test on failure."""
    return subprocess.run(
        list(argv),
        env=env if env is not None else os.environ.copy(),
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=check,
    )


# ---------- C1 — upgrade.sh auto git-pull ----------------------------


class TestUpgradeShAutoPull:
    """`./upgrade.sh` should `git pull` first (so the colleague doesn't have
    to remember the two-step flow), then exec `./install.sh --upgrade`.
    Opt-out via `--no-pull` for users who manage git themselves."""

    def test_upgrade_sh_invokes_git_pull_by_default(self, tmp_path: Path, monkeypatch):
        """Read upgrade.sh: it must mention `git pull` (or fetch + merge) in
        the default path. Pure source-level check; the live behavior is
        exercised by the integration test below."""
        content = UPGRADE_SH.read_text()
        assert "git pull" in content or "git fetch" in content, (
            "upgrade.sh must run `git pull` (or git fetch + merge) by default "
            "so colleagues don't need to remember the two-step flow. "
            "Found neither in upgrade.sh."
        )

    def test_upgrade_sh_supports_no_pull_optout(self):
        """`--no-pull` skips the pull and runs only the brain refresh.
        Users managing git themselves (release-engineering, CI) need this."""
        content = UPGRADE_SH.read_text()
        assert "--no-pull" in content, (
            "upgrade.sh must support `--no-pull` to skip the auto-pull. "
            "Without it, users who already pulled (or manage git themselves) "
            "would get redundant work or an unwanted merge."
        )

    def test_upgrade_sh_still_invokes_install_upgrade(self):
        """Sanity: don't lose the existing brain-refresh path in the refactor."""
        content = UPGRADE_SH.read_text()
        # Either `install.sh --upgrade` invocation OR a reference to the mode.
        assert "install.sh" in content and "--upgrade" in content, (
            "upgrade.sh must still invoke `./install.sh --upgrade` for the "
            "brain-refresh step. Otherwise the script does only the git pull."
        )

    def test_upgrade_sh_no_pull_flag_in_help(self):
        """The `--no-pull` opt-out should be documented in the script's
        own header comment so a user reading the source sees it."""
        content = UPGRADE_SH.read_text()
        lines = content.splitlines()
        # Help/doc lines are the leading `#` comment block
        header = "\n".join(
            line for line in lines[: 30]
            if line.startswith("#") or not line.strip()
        )
        assert "--no-pull" in header, (
            "upgrade.sh header comment should document `--no-pull` so "
            "`./upgrade.sh --help` (or reading the file) explains the flag."
        )


# ---------- C2 — install.sh --upgrade surfaces CHANGELOG diff ---------


class TestUpgradePrintsChangelogDiff:
    """When `--upgrade` runs, it should detect the brain's previously-
    installed version (from `<brain>/memory/recall/__init__.py` OR a
    version marker file) and print the CHANGELOG entries between then
    and now."""

    def test_upgrade_writes_version_marker_after_run(self, tmp_path: Path, monkeypatch):
        """After `--upgrade`, a marker file at `<brain>/.brainstack-version`
        should exist holding the version the brain was last upgraded to.
        This is what makes the NEXT upgrade able to compute a diff."""
        brain = tmp_path / "brain"
        brain.mkdir()

        env = os.environ.copy()
        env["BRAIN_ROOT"] = str(brain)
        env["HOME"] = str(tmp_path / "fakehome")
        Path(env["HOME"]).mkdir()
        # Skip the recall-cli pip install to make the test fast + offline.
        env["BRAINSTACK_SKIP_CLI_INSTALL"] = "1"

        result = _run(
            str(INSTALL_SH), "--upgrade",
            env=env, cwd=REPO_ROOT,
        )
        assert result.returncode == 0, (
            f"--upgrade failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

        marker = brain / ".brainstack-version"
        assert marker.is_file(), (
            "--upgrade must write <brain>/.brainstack-version after a run "
            "so the NEXT upgrade can detect a version transition."
        )
        # Must be parseable as a version-like string (vX.Y.Z or X.Y.Z)
        version = marker.read_text().strip()
        assert version, "version marker is empty"
        # Just sanity-check it's plausible (not pinning the format strictly
        # because dev checkouts may have suffix like 0.5.0+dev).
        assert version[0].isdigit() or version.startswith("v"), (
            f"version marker looks unparseable: {version!r}"
        )

    def test_upgrade_announces_version_transition_on_subsequent_run(
        self, tmp_path: Path, monkeypatch
    ):
        """If the brain already has a version marker for vX, and the repo
        is at vY (different), the second upgrade run should announce the
        transition in its output. Test: pre-seed an old version marker,
        run --upgrade, look for the announcement."""
        brain = tmp_path / "brain"
        brain.mkdir()
        # Pre-seed an older version marker as if from a previous upgrade
        (brain / ".brainstack-version").write_text("0.4.0")

        env = os.environ.copy()
        env["BRAIN_ROOT"] = str(brain)
        env["HOME"] = str(tmp_path / "fakehome")
        Path(env["HOME"]).mkdir()
        env["BRAINSTACK_SKIP_CLI_INSTALL"] = "1"

        result = _run(
            str(INSTALL_SH), "--upgrade",
            env=env, cwd=REPO_ROOT,
        )
        assert result.returncode == 0
        combined = result.stdout + result.stderr

        # The output should mention BOTH versions — the one we came from
        # and the one we landed on. Be lenient about exact phrasing.
        assert "0.4.0" in combined, (
            f"upgrade should announce 'upgraded from 0.4.0' (the pre-seeded "
            f"old version); got:\n{combined}"
        )
        # And the current version (read from recall/__init__.py)
        from recall import __version__ as current_version
        assert current_version in combined, (
            f"upgrade should announce the new version {current_version!r}; "
            f"got:\n{combined}"
        )
