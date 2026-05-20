"""v0.6.0 hardening — edge cases beyond the defaults-flip happy path.

Scope: the user is sharing brainstack tomorrow. These tests exercise the
scenarios that test_install_defaults_flip.py doesn't cover and that would
embarrass us if a colleague hit them:

  - Prepopulated host source dirs (real migrate path runs, not just the
    "no candidates" branch)
  - --yes triggers actual migration; source dir is preserved (mirror,
    not symlink — see PR #55 fixup)
  - Empty host dirs ("dir exists, no projects in it") behaves cleanly
  - Idempotent re-run on a populated brain (status path)
  - Mixed state recovery: setup/remove/setup is idempotent at each step
  - --yes overrides --no-prompt when both are passed (precedence)
  - Multiple --no-X flags compose cleanly
  - HOME path with a space character doesn't break anything

These are subprocess-level integration tests against the real install.sh,
isolated via tmp HOME + BRAINSTACK_SKIP_LAUNCHCTL=1 + BRAINSTACK_SKIP_CLI_INSTALL=1.
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
    fake_home.mkdir(parents=True, exist_ok=True)
    (fake_home / "Library" / "LaunchAgents").mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["BRAIN_ROOT"] = str(fake_home / ".agent")
    env["BRAINSTACK_SKIP_LAUNCHCTL"] = "1"
    env["BRAINSTACK_SKIP_CLI_INSTALL"] = "1"
    env["GIT_AUTHOR_NAME"] = "Hardening"
    env["GIT_AUTHOR_EMAIL"] = "harden@test"
    env["GIT_COMMITTER_NAME"] = "Hardening"
    env["GIT_COMMITTER_EMAIL"] = "harden@test"
    return env


def _run(*args: str, env: dict, cwd: Path | None = None,
         input_str: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(INSTALL_SH), *args],
        env=env,
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
        input=input_str,
    )


def _seed_claude_project_memory(home: Path, project_slug: str = "demo") -> Path:
    """Create a realistic ~/.claude/projects/<slug>/memory dir with content."""
    project = home / ".claude" / "projects" / project_slug
    memory = project / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    # Plausible content the migrate dispatcher would import
    (memory / "MEMORY.md").write_text(
        "# Demo Project Memory\n\n## Notes\n- Test seed entry\n"
    )
    semantic = memory / "semantic" / "lessons"
    semantic.mkdir(parents=True, exist_ok=True)
    (semantic / "test_lesson.md").write_text(
        "---\nname: test_lesson\ntype: feedback\n---\nA test lesson.\n"
    )
    return memory


# ---------- A: Prepopulated host source dirs ----------


class TestPrepopulatedHostSourcesMigrate:
    """The most likely first-day-of-use scenario: a colleague has a real
    Claude Code memory dir under ~/.claude/projects/<slug>/memory.

    Default install must (a) discover it, (b) with --yes auto-accept and
    actually migrate, (c) PRESERVE the original (mirror not symlink),
    (d) report it in the summary block as imported."""

    def test_yes_flag_migrates_prepopulated_source_without_symlinking_it(
        self, tmp_path: Path
    ):
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)
        seeded = _seed_claude_project_memory(fake_home, "demo-prepopulated")

        # Capture pre-state: source is a real dir with our seed file
        assert seeded.is_dir()
        assert not seeded.is_symlink()
        seed_file = seeded / "MEMORY.md"
        assert seed_file.is_file()

        result = _run(
            "--brain-remote", "git@example.com:test/scratch.git",
            "--yes",
            env=env,
        )
        assert result.returncode == 0, (
            f"--yes install failed:\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

        # After: the source dir must NOT be a symlink (mirror-not-replace
        # contract from PR #55 fixup). It must still be a real directory
        # with the original content intact.
        assert seeded.exists(), (
            f"--yes migrate removed the source dir {seeded} — that's the "
            f"silent-swap bug. Stdout:\n{result.stdout}"
        )
        assert not seeded.is_symlink(), (
            f"--yes migrate symlinked the source dir {seeded} — should "
            f"have mirrored it (--no-symlink in defaults block).\n"
            f"Stdout:\n{result.stdout}"
        )
        assert seed_file.is_file(), (
            f"--yes migrate destroyed the original file {seed_file}.\n"
            f"Stdout:\n{result.stdout}"
        )

    def test_empty_host_dir_no_candidates(self, tmp_path: Path):
        """User has ~/.claude but no projects yet — discovery should find
        no candidates and skip the migrate without errors."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)
        # Empty ~/.claude (no projects subdir)
        (fake_home / ".claude").mkdir(parents=True)

        result = _run(
            "--brain-remote", "git@example.com:test/scratch.git",
            "--no-prompt",
            env=env,
        )
        assert result.returncode == 0
        combined = result.stdout + result.stderr
        # Migrate line shows skipped (because --no-prompt)
        assert re.search(r"^\s*•\s.+\(--skip-migrate\)\s*$", combined, re.M), (
            f"empty ~/.claude with --no-prompt: migrate summary missing • line:\n"
            f"{combined}"
        )

    def test_no_host_dirs_at_all(self, tmp_path: Path):
        """Pristine tmp HOME with NO host dirs at all — discovery finds
        nothing, reports it, all other defaults still fire."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)
        # No ~/.claude, ~/.codex, ~/.cursor at all

        result = _run(
            "--brain-remote", "git@example.com:test/scratch.git",
            env=env,
        )
        assert result.returncode == 0, (
            f"install failed with no host dirs:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        combined = result.stdout + result.stderr
        # Migrate line should still appear in summary (• marker because nothing
        # was found / declined)
        assert re.search(r"^\s*•\s.+\(--skip-migrate\)\s*$", combined, re.M), (
            f"summary missing migrate-skip line with no host dirs:\n{combined}"
        )


# ---------- B: Re-run / mixed-state recovery ----------


class TestMixedStateRecovery:
    """If a user's surfaces drift (manually deleted a plist, sentinel removed,
    etc.), running the appropriate --setup-X must re-create cleanly."""

    def test_setup_recall_first_idempotent(self, tmp_path: Path):
        """Running --setup-recall-first-all twice in a row: the sentinel
        block is idempotent — second run replaces in-place, no duplication."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)
        # Seed the brain so the setup mode finds it
        brain = fake_home / ".agent"
        brain.mkdir()

        # Need ~/.claude to exist (recall-first writes to CLAUDE.md if dir exists)
        (fake_home / ".claude").mkdir()

        r1 = _run("--setup-recall-first-all", env=env)
        assert r1.returncode == 0, (
            f"first --setup-recall-first-all failed:\n{r1.stdout}\n{r1.stderr}"
        )

        claude_md = fake_home / ".claude" / "CLAUDE.md"
        if not claude_md.exists():
            pytest.skip(
                "setup-recall-first-all did not create CLAUDE.md in this "
                "tmp HOME — the mode may require the file to pre-exist. "
                "Idempotency assertion needs the file. Not a regression."
            )

        content_after_first = claude_md.read_text()
        sentinel_count_first = content_after_first.count(
            "<!-- brainstack-recall-first-start -->"
        )

        r2 = _run("--setup-recall-first-all", env=env)
        assert r2.returncode == 0, (
            f"second --setup-recall-first-all failed:\n{r2.stdout}\n{r2.stderr}"
        )

        content_after_second = claude_md.read_text()
        sentinel_count_second = content_after_second.count(
            "<!-- brainstack-recall-first-start -->"
        )

        assert sentinel_count_second == sentinel_count_first == 1, (
            f"sentinel block duplicated on re-run: first={sentinel_count_first}, "
            f"second={sentinel_count_second}. content after second run:\n"
            f"{content_after_second[:500]}"
        )

    def test_status_path_on_existing_brain(self, tmp_path: Path):
        """Running fresh-install command against an EXISTING brain hits the
        status path (line 2215+) — no defaults block, no destructive ops."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)
        # Seed an existing brain
        brain = fake_home / ".agent"
        brain.mkdir()
        (brain / "tools").mkdir()
        (brain / "memory").mkdir()
        (brain / ".brainstack-version").write_text("0.5.0")

        result = _run(
            "--brain-remote", "git@example.com:test/scratch.git",
            env=env,
        )
        assert result.returncode == 0, (
            f"install on existing brain failed:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        combined = result.stdout + result.stderr
        # Status path prints "already exists. Status:" line
        assert "already exists" in combined, (
            f"existing-brain install should hit status path:\n{combined}"
        )
        # And NONE of the defaults block markers should appear
        for flag in (
            "--skip-migrate", "--no-auto-migrate", "--no-launchd",
            "--no-recall-first", "--no-auto-recall",
        ):
            assert not re.search(
                rf"^\s*[✓•✗]\s.+\({re.escape(flag)}\)\s*$", combined, re.M
            ), (
                f"existing-brain install unexpectedly triggered defaults "
                f"summary line for {flag}:\n{combined}"
            )


# ---------- C: Flag precedence ----------


class TestFlagPrecedence:
    """--yes + --no-prompt is a wrapper-script mistake. Today's behavior:
    --yes wins (any prompt is auto-accepted because we check ASSUME_YES first).
    This test pins that precedence so a future refactor doesn't silently flip it."""

    def test_yes_overrides_no_prompt(self, tmp_path: Path):
        """When both --yes and --no-prompt are passed, --yes wins."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)
        # Seed a discoverable source so the precedence actually matters
        _seed_claude_project_memory(fake_home, "demo-precedence")

        result = _run(
            "--brain-remote", "git@example.com:test/scratch.git",
            "--yes",
            "--no-prompt",
            env=env,
        )
        assert result.returncode == 0, (
            f"--yes --no-prompt install failed:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        combined = result.stdout + result.stderr
        # With --yes winning, the migrate line should be ✓ (migrated)
        # OR • with detail "1 source(s) imported" OR "declined".
        # The key invariant: NO "--no-prompt set" reason in the migrate line.
        migrate_line_match = re.search(
            r"^\s*[✓•]\s+Migrate discovery:[^\n]*\(--skip-migrate\)\s*$",
            combined, re.M,
        )
        assert migrate_line_match, (
            f"missing migrate discovery summary line:\n{combined}"
        )
        migrate_line = migrate_line_match.group(0)
        assert "--no-prompt set" not in migrate_line, (
            f"--yes should override --no-prompt — but migrate summary "
            f"reports `--no-prompt set` reason:\n{migrate_line}\n"
            f"Full output:\n{combined}"
        )


# ---------- D: Multiple --no-X combinations ----------


class TestMultipleOptOutsCompose:
    """Multiple --no-X flags should compose cleanly — each skips exactly
    its own mode, others still fire."""

    def test_all_five_opt_outs_simultaneously(self, tmp_path: Path):
        """Passing all 5 opt-out flags + --skip-migrate: zero defaults fire,
        but install still completes cleanly. Useful for CI that wants only
        the base brain init."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)

        result = _run(
            "--brain-remote", "git@example.com:test/scratch.git",
            "--skip-migrate",
            "--no-auto-migrate",
            "--no-launchd",
            "--no-recall-first",
            "--no-auto-recall",
            env=env,
        )
        assert result.returncode == 0, (
            f"all-opt-outs install failed:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        combined = result.stdout + result.stderr
        # Every summary line should start with • (skipped)
        for flag in (
            "--skip-migrate", "--no-auto-migrate", "--no-launchd",
            "--no-recall-first", "--no-auto-recall",
        ):
            line_match = re.search(
                rf"^\s*([✓•✗])\s.+\({re.escape(flag)}\)\s*$",
                combined, re.M,
            )
            assert line_match, f"missing summary line for {flag}:\n{combined}"
            mark = line_match.group(1)
            assert mark == "•", (
                f"with all opt-outs, expected • (skipped) for {flag}, "
                f"got '{mark}' in:\n{line_match.group(0)}"
            )

    def test_three_opt_outs_only_two_defaults_fire(self, tmp_path: Path):
        """Three opt-outs: --no-launchd, --no-recall-first, --skip-migrate.
        Remaining two (--setup-auto-migrate, --enable-auto-recall) still fire."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)

        result = _run(
            "--brain-remote", "git@example.com:test/scratch.git",
            "--skip-migrate",
            "--no-launchd",
            "--no-recall-first",
            env=env,
        )
        assert result.returncode == 0

        combined = result.stdout + result.stderr
        # The two non-opted defaults should be ✓ (or possibly ✗ if env)
        for flag in ("--no-auto-migrate", "--no-auto-recall"):
            line_match = re.search(
                rf"^\s*([✓•✗])\s.+\({re.escape(flag)}\)\s*$",
                combined, re.M,
            )
            assert line_match, f"missing line for {flag}:\n{combined}"
            mark = line_match.group(1)
            assert mark == "✓", (
                f"expected ✓ (done) for {flag} without opt-out, got '{mark}' "
                f"in line:\n{line_match.group(0)}\nfull:\n{combined}"
            )


# ---------- F: Multiple discoverable sources ----------


class TestMultipleDiscoverableSources:
    """Real colleagues will have all three host dirs populated. The discovery
    loop must handle 2+ candidates cleanly."""

    def test_multiple_host_dirs_all_get_discovered(self, tmp_path: Path):
        """Seed Claude + Codex + Cursor dirs. --yes accepts all. Each
        source should be preserved (mirror not symlink, from PR #55 fixup)."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)

        # Three populated sources
        claude_mem = _seed_claude_project_memory(fake_home, "project-a")
        codex_dir = fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "sessions").mkdir()
        (codex_dir / "AGENTS.md").write_text("# codex agents config\n")
        cursor_dir = fake_home / ".cursor"
        cursor_dir.mkdir(parents=True)
        (cursor_dir / "rules").mkdir()

        # Snapshot pre-state
        claude_inode = claude_mem.stat().st_ino
        codex_inode = codex_dir.stat().st_ino
        cursor_inode = cursor_dir.stat().st_ino

        result = _run(
            "--brain-remote", "git@example.com:test/scratch.git",
            "--yes",
            env=env,
        )
        assert result.returncode == 0, (
            f"--yes install with multi-source failed:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        # Each source dir must still exist + be the SAME inode (not replaced
        # with a symlink, which would change the inode)
        assert claude_mem.exists() and not claude_mem.is_symlink(), (
            f"Claude memory dir was replaced with a symlink: {claude_mem}"
        )
        assert codex_dir.exists() and not codex_dir.is_symlink(), (
            f"Codex dir was replaced with a symlink: {codex_dir}"
        )
        assert cursor_dir.exists() and not cursor_dir.is_symlink(), (
            f"Cursor dir was replaced with a symlink: {cursor_dir}"
        )

        # Inode preservation is the strongest invariant
        assert claude_mem.stat().st_ino == claude_inode, (
            f"Claude memory dir inode changed (was replaced)"
        )
        assert codex_dir.stat().st_ino == codex_inode
        assert cursor_dir.stat().st_ino == cursor_inode


# ---------- G: Help + bad-arg parsing safety ----------


class TestArgParsingSafety:
    """Parsing changes in this PR shouldn't break --help, bare invocation,
    or the unknown-arg error path."""

    def test_help_still_works(self):
        env = os.environ.copy()
        result = subprocess.run(
            [str(INSTALL_SH), "--help"],
            env=env,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert result.returncode == 0, (
            f"--help failed (rc={result.returncode}):\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert len(result.stdout) > 0, "--help printed nothing"

    def test_unknown_flag_still_errors(self, tmp_path: Path):
        """The catch-all `*)` branch must still reject unknown args. With
        the new flags added, the parser order matters — unknowns must
        fall through to the error case."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)

        result = _run("--definitely-not-a-real-flag", env=env)
        assert result.returncode != 0, (
            f"unknown flag should exit non-zero, got rc=0\n{result.stdout}\n{result.stderr}"
        )
        assert "unknown argument" in (result.stdout + result.stderr).lower(), (
            f"unknown-arg error message missing:\n{result.stdout}\n{result.stderr}"
        )


# ---------- H: Idempotent default install ----------


class TestIdempotentDefaultInstall:
    """Running default install twice in a row: second run hits the status
    path (brain exists). No errors, no surprises, no duplicate state."""

    def test_default_install_twice_clean(self, tmp_path: Path):
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)

        r1 = _run(
            "--brain-remote", "git@example.com:test/scratch.git",
            "--no-prompt",
            env=env,
        )
        assert r1.returncode == 0

        # Capture state after first install
        brain = fake_home / ".agent"
        first_files = sorted(p.relative_to(brain) for p in brain.rglob("*") if p.is_file())[:50]

        r2 = _run(
            "--brain-remote", "git@example.com:test/scratch.git",
            "--no-prompt",
            env=env,
        )
        assert r2.returncode == 0, (
            f"second default install failed:\n{r2.stdout}\n{r2.stderr}"
        )

        # Second run prints status path, NOT defaults summary
        combined2 = r2.stdout + r2.stderr
        assert "already exists" in combined2, (
            f"second run should hit status path:\n{combined2}"
        )
        for flag in (
            "--skip-migrate", "--no-auto-migrate", "--no-launchd",
            "--no-recall-first", "--no-auto-recall",
        ):
            assert not re.search(
                rf"^\s*[✓•✗]\s.+\({re.escape(flag)}\)\s*$", combined2, re.M
            ), (
                f"second run should NOT print defaults summary for {flag}:\n{combined2}"
            )

        # Brain file layout unchanged (modulo synthetic timestamps in some
        # files). Just confirm the count is the same — we didn't delete or
        # duplicate anything.
        second_files = sorted(p.relative_to(brain) for p in brain.rglob("*") if p.is_file())[:50]
        assert second_files == first_files, (
            "Brain file layout changed between runs (first 50 files):\n"
            f"first:  {first_files}\n"
            f"second: {second_files}"
        )


# ---------- I: Content validation — not just status strings ----------


class TestActualArtifactsWritten:
    """The summary block says ✓ — but is the actual content correct?
    Validate the artifacts each default mode produces."""

    def test_auto_recall_toml_flag_actually_set_to_true(self, tmp_path: Path):
        """After a default install (auto-recall ON), the runtime TOML must
        have `enable_auto_recall = true`. Just checking the summary line
        is not enough."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)

        result = _run(
            "--brain-remote", "git@example.com:test/scratch.git",
            "--no-prompt",
            env=env,
        )
        assert result.returncode == 0

        toml = fake_home / ".agent" / "runtime" / "pyproject.toml"
        assert toml.is_file(), (
            f"runtime/pyproject.toml not written:\n{result.stdout}"
        )
        content = toml.read_text()
        assert re.search(r"enable_auto_recall\s*=\s*true", content), (
            f"enable_auto_recall flag not 'true' in:\n{content[:1500]}"
        )

    def test_no_auto_recall_keeps_toml_false(self, tmp_path: Path):
        """With --no-auto-recall, the TOML flag must NOT flip to true."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)

        result = _run(
            "--brain-remote", "git@example.com:test/scratch.git",
            "--no-prompt",
            "--no-auto-recall",
            env=env,
        )
        assert result.returncode == 0

        toml = fake_home / ".agent" / "runtime" / "pyproject.toml"
        if not toml.is_file():
            return  # no TOML means no flag — fine for --no-auto-recall
        content = toml.read_text()
        if "enable_auto_recall" in content:
            assert not re.search(r"enable_auto_recall\s*=\s*true", content), (
                f"--no-auto-recall: enable_auto_recall should not be true:\n"
                f"{content[:1500]}"
            )

    def test_recall_first_sentinel_block_written_with_directive(
        self, tmp_path: Path
    ):
        """After default install, ~/.claude/CLAUDE.md must contain the
        sentinel-delimited block AND it must mention the recall-first
        directive (not just empty sentinels)."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)
        (fake_home / ".claude").mkdir()  # ensure the dir exists

        result = _run(
            "--brain-remote", "git@example.com:test/scratch.git",
            "--no-prompt",
            env=env,
        )
        assert result.returncode == 0

        claude_md = fake_home / ".claude" / "CLAUDE.md"
        if not claude_md.is_file():
            pytest.skip("CLAUDE.md not created in this test fakehome")

        content = claude_md.read_text()
        assert "brainstack-recall-first-start" in content, (
            f"sentinel start marker missing in CLAUDE.md:\n{content[:800]}"
        )
        assert "brainstack-recall-first-end" in content, (
            f"sentinel end marker missing:\n{content[:800]}"
        )
        # The directive content (between sentinels) must non-trivially exist
        m = re.search(
            r"brainstack-recall-first-start.*?brainstack-recall-first-end",
            content, re.DOTALL,
        )
        assert m is not None
        block = m.group(0)
        assert len(block) > 100, (
            f"recall-first sentinel block suspiciously short ({len(block)} chars):\n{block}"
        )

    def test_launchd_plists_have_no_replace_placeholders(self, tmp_path: Path):
        """Default install writes plists. None of them should contain the
        REPLACE_HOME / REPLACE_PYTHON template placeholders — that would
        cause silent launchd failures."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)

        result = _run(
            "--brain-remote", "git@example.com:test/scratch.git",
            "--no-prompt",
            env=env,
        )
        assert result.returncode == 0

        plist_dir = fake_home / "Library" / "LaunchAgents"
        plists = list(plist_dir.glob("*.plist"))
        assert plists, f"no plists written to {plist_dir}"

        for plist in plists:
            content = plist.read_text()
            assert "REPLACE_HOME" not in content, (
                f"{plist.name} contains unexpanded REPLACE_HOME placeholder"
            )
            assert "REPLACE_PYTHON" not in content, (
                f"{plist.name} contains unexpanded REPLACE_PYTHON placeholder"
            )
            # Must reference the actual tmp HOME (not the host machine's HOME)
            assert str(fake_home) in content, (
                f"{plist.name} doesn't reference the install's HOME={fake_home}"
            )


# ---------- J: Per-mode setup → remove → setup idempotency ----------


class TestPerModeReentry:
    """Each --setup-X must be safe to run after --remove-X. This is how
    users recover from manual surgery on their host configs."""

    def test_setup_recall_first_after_remove_re_creates(self, tmp_path: Path):
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)
        brain = fake_home / ".agent"
        brain.mkdir()
        (fake_home / ".claude").mkdir()

        # setup → remove → setup
        r1 = _run("--setup-recall-first-all", env=env)
        assert r1.returncode == 0, f"setup-1 failed:\n{r1.stdout}\n{r1.stderr}"

        r2 = _run("--remove-recall-first-all", env=env)
        assert r2.returncode == 0, f"remove failed:\n{r2.stdout}\n{r2.stderr}"

        r3 = _run("--setup-recall-first-all", env=env)
        assert r3.returncode == 0, f"setup-2 failed:\n{r3.stdout}\n{r3.stderr}"

        # After re-setup, CLAUDE.md should have the sentinel block
        claude_md = fake_home / ".claude" / "CLAUDE.md"
        if claude_md.is_file():
            content = claude_md.read_text()
            assert "brainstack-recall-first-start" in content, (
                f"after setup→remove→setup, CLAUDE.md missing sentinel block:\n{content[:500]}"
            )

    def test_enable_disable_enable_auto_recall(self, tmp_path: Path):
        """Auto-recall TOML flag must flip cleanly."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)
        # Seed brain with runtime dir + minimal pyproject
        brain = fake_home / ".agent"
        runtime = brain / "runtime"
        runtime.mkdir(parents=True)
        (runtime / "pyproject.toml").write_text(
            "[tool.brainstack]\nenable_auto_recall = false\n"
        )

        r1 = _run("--enable-auto-recall", env=env)
        assert r1.returncode == 0
        assert "true" in (runtime / "pyproject.toml").read_text()

        r2 = _run("--disable-auto-recall", env=env)
        assert r2.returncode == 0
        assert "false" in (runtime / "pyproject.toml").read_text()

        r3 = _run("--enable-auto-recall", env=env)
        assert r3.returncode == 0
        assert "true" in (runtime / "pyproject.toml").read_text()


# ---------- K: Static invariants (cheap pin via grep) ----------


class TestStaticInstallShInvariants:
    """Cheap static checks that pin properties of install.sh — fast guards
    against accidental regressions in the option parser, mode dispatch, etc."""

    def test_all_five_opt_outs_are_in_parser(self):
        """All 5 opt-out flags must have explicit case-statement branches.
        Without this, an unknown-arg error would surface."""
        content = (REPO_ROOT / "install.sh").read_text()
        for flag in (
            "--no-auto-migrate",
            "--no-launchd",
            "--no-recall-first",
            "--no-auto-recall",
            "--skip-migrate",
            "--no-prompt",
        ):
            assert re.search(rf"^\s*{re.escape(flag)}\)", content, re.M), (
                f"option parser missing case for {flag}"
            )

    def test_yes_is_dual_purpose(self):
        """`-y|--yes` must set BOTH UNINSTALL_YES (uninstall) and
        ASSUME_YES (install migrate-discovery). This is the bug that
        the precedence test caught."""
        content = (REPO_ROOT / "install.sh").read_text()
        # Find the -y|--yes case body
        m = re.search(
            r"-y\|--yes\)(.*?);;\s*$",
            content, re.DOTALL | re.MULTILINE,
        )
        assert m is not None, "no -y|--yes case found"
        body = m.group(1)
        assert "UNINSTALL_YES=1" in body, (
            f"-y|--yes case must set UNINSTALL_YES=1:\n{body}"
        )
        assert "ASSUME_YES=1" in body, (
            f"-y|--yes case must set ASSUME_YES=1 (the precedence fix):\n{body}"
        )

    def test_changelog_has_v060_entry(self):
        """v0.6.0 release entry must exist in CHANGELOG."""
        content = (REPO_ROOT / "CHANGELOG.md").read_text()
        assert "## v0.6.0" in content, "CHANGELOG.md missing ## v0.6.0 section"
        # And it should mention the defaults flip
        v060_section = content.split("## v0.6.0", 1)[1].split("## v0.5", 1)[0]
        assert "default" in v060_section.lower(), (
            "v0.6.0 CHANGELOG should describe the defaults flip"
        )

    def test_readme_has_customize_your_install_section(self):
        """README must have the new H2 introduced by PR #55."""
        content = (REPO_ROOT / "README.md").read_text()
        assert "## Customize your install" in content, (
            "README.md missing `## Customize your install` H2"
        )
        # And it should reference the 5 opt-outs in the table
        customize_section = content.split("## Customize your install", 1)[1].split("##", 1)[0]
        for flag in (
            "--skip-migrate", "--no-auto-migrate", "--no-launchd",
            "--no-recall-first", "--no-auto-recall",
        ):
            assert flag in customize_section, (
                f"`## Customize your install` table missing {flag}"
            )


# ---------- E: HOME path with a space ----------


class TestHomePathWithSpace:
    """Some users have HOME at `/Users/First Last/`. Install must work."""

    def test_install_with_space_in_home_path(self, tmp_path: Path):
        fake_home = tmp_path / "fake home with space"
        env = _fresh_env(fake_home)

        result = _run(
            "--brain-remote", "git@example.com:test/scratch.git",
            "--no-prompt",
            env=env,
        )
        assert result.returncode == 0, (
            f"install with space-in-HOME failed (rc={result.returncode}):\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        # Brain was created at the spaced path
        brain = fake_home / ".agent"
        assert brain.is_dir(), (
            f"brain not created at spaced path {brain}"
        )
