"""Defaults flip — install.sh runs five setup modes by default on fresh installs.

User: "can a lot of these be auto enabled? So the user uses all of them and
then can opt out individually as needed."

Five modes that were opt-in in 0.5.0 become default-on for fresh installs:

  1. Interactive migrate discovery (scan ~/.claude, ~/.codex, ~/.cursor dirs,
     prompt y/n for each found). Opt-out: --skip-migrate / --no-prompt.
  2. --setup-auto-migrate (background scanner LaunchAgent). Opt-out: --no-auto-migrate.
  3. --setup-launchd (hourly sync + nightly dream LaunchAgents). Opt-out: --no-launchd.
  4. --setup-recall-first-all (directive in 3 host files). Opt-out: --no-recall-first.
  5. --enable-auto-recall (Claude Code UserPromptSubmit hook TOML flag).
     Opt-out: --no-auto-recall.

Backward compatibility: --upgrade and explicit --setup-X invocations do NOT
trigger the defaults. Existing 0.5.0 users feel zero change.

The final stdout summary lists each fired/skipped default with its opt-out flag
in parens, so users see what was done and how to skip it next time.

Tests use tmp HOME + BRAINSTACK_SKIP_LAUNCHCTL=1 + BRAINSTACK_SKIP_CLI_INSTALL=1.

Consent gate (adoption-audit fix): the full default install now requires
explicit consent. Non-TTY runs without --yes fall back to --minimal, so
every fresh-install test below that expects the five defaults passes --yes.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"


def _fresh_env(fake_home: Path) -> dict:
    """Standard env for a fresh install invocation in tmp_path."""
    fake_home.mkdir(parents=True, exist_ok=True)
    (fake_home / "Library" / "LaunchAgents").mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["BRAIN_ROOT"] = str(fake_home / ".agent")
    env["BRAINSTACK_SKIP_LAUNCHCTL"] = "1"
    env["BRAINSTACK_SKIP_CLI_INSTALL"] = "1"
    # The fresh-install path runs `git init` + `git commit` for the brain
    # repo. Without an identity configured in the tmp HOME, the commit fails.
    # These env vars override config lookup, so tests don't need to seed a
    # ~/.gitconfig.
    env["GIT_AUTHOR_NAME"] = "Test Installer"
    env["GIT_AUTHOR_EMAIL"] = "test@example.com"
    env["GIT_COMMITTER_NAME"] = "Test Installer"
    env["GIT_COMMITTER_EMAIL"] = "test@example.com"
    return env


def _run(*args: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(INSTALL_SH), *args],
        env=env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )


def _summary_line(combined: str, opt_out_flag: str) -> str | None:
    """Find the summary line that mentions a specific opt-out flag.

    The summary prints either:
      ✓ <description> (--no-X)         — when the default fired
      • Skipped <thing> (--no-X)       — when --no-X was passed

    Returns the matching line, or None if not found.
    """
    pattern = re.compile(rf"^\s*[✓•]\s.+\({re.escape(opt_out_flag)}\)\s*$", re.M)
    m = pattern.search(combined)
    return m.group(0) if m else None


# ---------- Fresh install: all 5 defaults fire ----------


class TestFreshInstallRunsAllFiveDefaults:
    """A fresh install (no explicit mode flag, only --brain-remote +
    --push-initial-commit) triggers all 5 default modes after the base install.
    The summary block lists each one with its opt-out flag."""

    def test_fresh_install_runs_all_five_defaults(self, tmp_path: Path):
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)

        # --yes consents to the full install (non-TTY without it falls back
        # to --minimal). With no discoverable sources in the clean tmp HOME,
        # migrate discovery reports skipped; the OTHER 4 defaults fire.
        result = _run(
            "--brain-remote", "git@example.com:test/scratch.git",
            "--yes",
            env=env,
        )
        assert result.returncode == 0, (
            f"fresh install failed (rc={result.returncode}):\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        combined = result.stdout + result.stderr

        # Each default has a summary line referencing its opt-out flag.
        # ✓ means default fired; • means skipped (e.g. --no-prompt skips migrate).
        for flag in (
            "--skip-migrate",       # migrate discovery
            "--no-auto-migrate",    # background scanner
            "--no-launchd",          # hourly sync + nightly dream
            "--no-recall-first",     # directive in 3 host files
            "--no-auto-recall",      # claude code hook
        ):
            assert _summary_line(combined, flag) is not None, (
                f"summary block missing line for {flag}:\n{combined}"
            )


# ---------- Per-opt-out: --no-X skips that mode, others still fire ----------


class TestOptOutFlagsIsolated:
    """Each --no-X opt-out skips ONLY its own default; the other 4 still fire."""

    @pytest.mark.parametrize("opt_out", [
        "--no-auto-migrate",
        "--no-launchd",
        "--no-recall-first",
        "--no-auto-recall",
    ])
    def test_opt_out_marks_just_that_one_as_skipped(self, tmp_path: Path, opt_out: str):
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)

        result = _run(
            "--brain-remote", "git@example.com:test/scratch.git",
            "--yes",
            opt_out,
            env=env,
        )
        assert result.returncode == 0, (
            f"install with {opt_out} failed:\n{result.stdout}\n{result.stderr}"
        )

        combined = result.stdout + result.stderr

        # The opted-out flag's line should start with • (skipped marker)
        skipped_line = _summary_line(combined, opt_out)
        assert skipped_line is not None, (
            f"summary missing line for {opt_out}:\n{combined}"
        )
        assert skipped_line.lstrip().startswith("•"), (
            f"with {opt_out}, summary line should start with • (skipped), got:\n"
            f"{skipped_line!r}\nfull output:\n{combined}"
        )

        # The OTHER 4 defaults should still appear (with ✓ or •) — verify
        # we didn't accidentally short-circuit the whole block on one opt-out.
        other_flags = {
            "--no-auto-migrate", "--no-launchd",
            "--no-recall-first", "--no-auto-recall",
        } - {opt_out}
        for other in other_flags:
            assert _summary_line(combined, other) is not None, (
                f"{opt_out} should not affect {other}'s summary line. "
                f"Output:\n{combined}"
            )

    def test_skip_migrate_skips_discovery_explicitly(self, tmp_path: Path):
        """--skip-migrate is the migrate-specific opt-out (since migrate
        discovery is the only INTERACTIVE default)."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)

        result = _run(
            "--brain-remote", "git@example.com:test/scratch.git",
            "--yes",
            "--skip-migrate",
            env=env,
        )
        assert result.returncode == 0

        combined = result.stdout + result.stderr
        # --skip-migrate produces a • line referencing the flag
        line = _summary_line(combined, "--skip-migrate")
        assert line is not None and line.lstrip().startswith("•"), (
            f"--skip-migrate should produce a • summary line:\n{combined}"
        )


# ---------- Non-interactive: --yes / --no-prompt ----------


class TestNonInteractiveMigratePrompts:
    """--yes accepts all migrate prompts (auto-import discovered dirs);
    --no-prompt declines all of them. Either way no stdin needed."""

    def test_no_prompt_completes_without_stdin_hang(self, tmp_path: Path):
        """The interactive scan must not hang when --no-prompt is set —
        proven by the fact that this test runs to completion (subprocess
        has no stdin attached)."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)

        result = _run(
            "--brain-remote", "git@example.com:test/scratch.git",
            "--yes",
            "--no-prompt",
            env=env,
        )
        assert result.returncode == 0, (
            f"--no-prompt should complete cleanly; got rc={result.returncode}\n"
            f"{result.stdout}\n{result.stderr}"
        )

    def test_yes_completes_without_stdin_hang(self, tmp_path: Path):
        """--yes also completes without stdin. With no discovered sources
        in a clean tmp HOME, --yes is a no-op for migrate; other 4 still fire."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)

        result = _run(
            "--brain-remote", "git@example.com:test/scratch.git",
            "--yes",
            env=env,
        )
        assert result.returncode == 0, (
            f"--yes should complete cleanly; got rc={result.returncode}\n"
            f"{result.stdout}\n{result.stderr}"
        )


# ---------- Backward compatibility: --upgrade / explicit modes don't trigger defaults ----------


class TestBackwardCompatibility:
    """Existing 0.5.0 users on --upgrade or explicit --setup-X must NOT see
    the new defaults fire. Defaults only apply on the fresh-install code path."""

    def test_upgrade_mode_does_not_trigger_defaults(self, tmp_path: Path):
        """`./install.sh --upgrade` is the 0.5.0 user's path. The five default
        marker lines must NOT appear in the upgrade summary."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)
        # Upgrade requires an existing brain — seed a minimal one
        brain = fake_home / ".agent"
        brain.mkdir()

        result = _run("--upgrade", env=env)
        assert result.returncode == 0, (
            f"--upgrade failed:\n{result.stdout}\n{result.stderr}"
        )

        combined = result.stdout + result.stderr
        # None of the default opt-out markers should appear in upgrade output
        for flag in (
            "--no-auto-migrate", "--no-launchd",
            "--no-recall-first", "--no-auto-recall", "--skip-migrate",
        ):
            assert _summary_line(combined, flag) is None, (
                f"--upgrade should not trigger the defaults block; "
                f"found unexpected summary line for {flag}:\n{combined}"
            )

    def test_explicit_setup_launchd_does_not_trigger_other_defaults(self, tmp_path: Path):
        """Calling `./install.sh --setup-launchd` directly is the explicit-mode
        path; only that mode runs, not the other 4 defaults."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)
        brain = fake_home / ".agent"
        brain.mkdir()

        result = _run("--setup-launchd", env=env)
        assert result.returncode == 0, (
            f"--setup-launchd failed:\n{result.stdout}\n{result.stderr}"
        )

        combined = result.stdout + result.stderr
        # The defaults summary block uses the (--no-X) markers — those must
        # NOT appear when invoking an explicit mode.
        for flag in (
            "--no-auto-migrate",
            "--no-recall-first", "--no-auto-recall", "--skip-migrate",
        ):
            assert _summary_line(combined, flag) is None, (
                f"--setup-launchd alone must not trigger the defaults block; "
                f"found unexpected summary line for {flag}:\n{combined}"
            )


# ---------- Idempotency / re-run safety ----------


class TestIdempotency:
    """Re-running install.sh on an existing brain should not double-write or
    re-trigger destructive operations."""

    def test_second_run_on_existing_brain_is_status_only(self, tmp_path: Path):
        """When BRAIN_ROOT already exists, install.sh takes the status path
        (line 2215-2273) — it does NOT re-run the defaults. This is the
        pre-existing behavior; we want to preserve it after the flip."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)

        # First run: fresh install fires defaults (--yes consents)
        r1 = _run(
            "--brain-remote", "git@example.com:test/scratch.git",
            "--yes",
            env=env,
        )
        assert r1.returncode == 0

        # Second run: no flags — status check, no defaults block
        r2 = _run(env=env)
        assert r2.returncode == 0

        combined2 = r2.stdout + r2.stderr
        # Status path doesn't print the defaults summary — none of the
        # (--no-X) markers should appear in the second run's output.
        for flag in (
            "--no-auto-migrate", "--no-launchd",
            "--no-recall-first", "--no-auto-recall", "--skip-migrate",
        ):
            assert _summary_line(combined2, flag) is None, (
                f"second run on existing brain unexpectedly re-triggered "
                f"defaults for {flag}:\n{combined2}"
            )


# ---------- Migrate discovery is non-destructive by default ----------


class TestMigrateDiscoveryDoesNotSymlinkSources:
    """The new default-on migrate discovery must NOT silently swap a user's
    ~/.claude/projects/*/memory for a symlink into the brain. That's
    destructive default behavior the user explicitly said they don't want.

    The defaults block passes --no-symlink when calling --migrate, so the
    source dir is mirrored (preserved as-is) instead of being backed up + replaced.

    Pinned at source level by grepping install.sh for the flag near the
    discovery loop — the install.sh logic is a recursive `$0 --migrate ...`
    call, so the flag has to be on that line.
    """

    def test_discovery_recursive_migrate_includes_no_symlink(self):
        """The defaults-block migrate-discovery loop must include --no-symlink
        on the recursive `$0 --migrate ...` call. This is the safety bar:
        users running the default install get mirror-not-replace behavior."""
        install_sh = (REPO_ROOT / "install.sh").read_text()

        # Find the defaults-block migrate discovery section (lives between
        # the `Default 1: interactive migrate discovery` marker and the
        # `Default 2: --setup-auto-migrate` marker).
        m = re.search(
            r"# --- Default 1: interactive migrate discovery ---(.*?)# --- Default 2:",
            install_sh,
            re.DOTALL,
        )
        assert m is not None, (
            "couldn't locate the defaults-block migrate discovery section "
            "in install.sh — the section markers may have been renamed"
        )
        discovery_block = m.group(1)

        # The recursive `$SELF --migrate` call inside this block MUST include
        # --no-symlink. Otherwise the default-on discovery silently swaps
        # native dirs for symlinks (the behavior the user wants to avoid).
        # ("$SELF" is the absolute-path handle the installer resolves once so
        # recursive sub-invocations work under `bash install.sh`; it replaced
        # the bare "$0".)
        assert re.search(r'"\$(?:0|SELF)"\s+--migrate\s+"\$src"\s+--no-symlink', discovery_block), (
            "default-on migrate discovery must call `$SELF --migrate $src --no-symlink` "
            "(mirror-not-replace). Found block (first 800 chars):\n"
            + discovery_block[:800]
        )


# ---------- Summary block UX ----------


class TestFinalSummaryStructure:
    """The summary block at the end of a fresh install must be human-readable
    and discoverable — each line shows what was done + the opt-out flag."""

    def test_summary_block_lists_all_five_modes(self, tmp_path: Path):
        """The summary block lists exactly the 5 modes with their opt-out flags."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)

        result = _run(
            "--brain-remote", "git@example.com:test/scratch.git",
            "--yes",
            env=env,
        )
        assert result.returncode == 0

        combined = result.stdout + result.stderr

        # All five opt-out flag markers present
        flags = ["--skip-migrate", "--no-auto-migrate", "--no-launchd",
                 "--no-recall-first", "--no-auto-recall"]
        lines = [_summary_line(combined, f) for f in flags]
        assert all(line is not None for line in lines), (
            f"summary must list all 5 modes; got:\n"
            + "\n".join(f"  {f}: {l!r}" for f, l in zip(flags, lines))
        )

    def test_summary_block_mentions_recall_doctor_and_uninstall(self, tmp_path: Path):
        """Summary points users to verification + removal paths."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)

        result = _run(
            "--brain-remote", "git@example.com:test/scratch.git",
            "--yes",
            env=env,
        )
        assert result.returncode == 0

        combined = result.stdout + result.stderr
        assert "recall doctor" in combined, (
            f"summary should mention `recall doctor` for verification:\n{combined}"
        )
        assert "./uninstall.sh" in combined or "uninstall" in combined.lower(), (
            f"summary should mention `./uninstall.sh` for removal:\n{combined}"
        )
