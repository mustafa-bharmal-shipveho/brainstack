"""End-to-end tests for `./uninstall.sh` (and the equivalent
`./install.sh --uninstall`).

Hermetic: every test spins up an isolated HOME in tmp_path with all the
brainstack surfaces pre-created (launchd plists, .zshrc lines, CLAUDE.md
blocks, etc.). Subprocesses run the actual scripts against that HOME.

Safety contracts pinned by these tests:

  1. Default `./uninstall.sh -y` removes host-config surfaces but
     PRESERVES the user's data at ~/.agent/, ~/.config/recall,
     ~/.config/brainstack.
  2. `--dry-run` touches nothing.
  3. `--purge-data` only removes data when explicitly passed.
  4. `./uninstall.sh` and `./install.sh --uninstall` produce identical
     outcomes (the former is a thin wrapper around the latter).
  5. Idempotent: re-running after uninstall doesn't error.
  6. Partial-install: missing surfaces don't cause failures.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install.sh"
UNINSTALL_SH = REPO_ROOT / "uninstall.sh"


def _run(script: Path, *args: str, env_overrides: dict[str, str] | None = None,
         expect_success: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    result = subprocess.run(
        [str(script), *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if expect_success and result.returncode != 0:
        raise AssertionError(
            f"{script.name} {args} failed:\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )
    return result


@pytest.fixture
def fake_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A HOME directory that LOOKS like brainstack was fully installed.

    Returns a dict of important paths so tests can assert their state
    before/after uninstall.
    """
    home = tmp_path / "fakehome"

    # Directory shells
    (home / ".claude").mkdir(parents=True)
    (home / ".codex").mkdir(parents=True)
    (home / ".cursor").mkdir(parents=True)
    (home / ".local" / "bin").mkdir(parents=True)
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    (home / ".cache" / "recall" / "qdrant").mkdir(parents=True)
    (home / ".config" / "recall").mkdir(parents=True)
    (home / ".config" / "brainstack").mkdir(parents=True)

    # The brain — sacrosanct, must survive default uninstall
    brain = home / ".agent"
    brain.mkdir(parents=True)
    (brain / "memory").mkdir()
    (brain / "memory" / "semantic").mkdir()
    (brain / "memory" / "semantic" / "lessons").mkdir()
    (brain / "memory" / "semantic" / "lessons" / "important.md").write_text(
        "# important lesson\n\nIRREPLACEABLE_USER_CONTENT\n"
    )

    # zshrc with the brainstack-shell-banner sentinel block
    zshrc = home / ".zshrc"
    zshrc.write_text(
        "# user pre-existing zsh content\n"
        "alias my-favorite=true\n"
        "# >>> brainstack-shell-banner >>>\n"
        '[ -f "$HOME/.agent/banner/brainstack-shell-banner.sh" ] && '
        'source "$HOME/.agent/banner/brainstack-shell-banner.sh"\n'
        "# <<< brainstack-shell-banner <<<\n"
        "# more user content below\n"
    )

    # Host instruction files with brainstack blocks
    (home / ".claude" / "CLAUDE.md").write_text(
        "# user's claude config\n\n"
        "<!-- brainstack-recall-first-start -->\n"
        "Use recall first.\n"
        "<!-- brainstack-recall-first-end -->\n"
        "\nuser content below the block\n"
    )
    (home / ".codex" / "AGENTS.md").write_text(
        "# user's codex config\n\n"
        "<!-- brainstack-recall-first-start -->\n"
        "Use recall first.\n"
        "<!-- brainstack-recall-first-end -->\n"
    )
    (home / ".cursor" / ".cursorrules").write_text(
        "# user's cursor rules\n\n"
        "<!-- brainstack-recall-first-start -->\n"
        "Use recall first.\n"
        "<!-- brainstack-recall-first-end -->\n"
    )

    # Launchd plists
    plist_dir = home / "Library" / "LaunchAgents"
    (plist_dir / "com.user.agent-dream.plist").write_text(
        "<?xml version='1.0'?><plist><dict></dict></plist>"
    )
    (plist_dir / "com.user.agent-sync.plist").write_text(
        "<?xml version='1.0'?><plist><dict></dict></plist>"
    )

    # PATH symlinks
    recall_bin = home / ".local" / "bin" / "recall"
    fake_target = home / "venv-fake" / "bin" / "recall"
    fake_target.parent.mkdir(parents=True, exist_ok=True)
    fake_target.write_text("#!/bin/sh\necho fake-recall\n")
    fake_target.chmod(0o755)
    recall_bin.symlink_to(fake_target)

    # Cache + configs
    (home / ".cache" / "recall" / "qdrant" / "marker").write_text("cache content")
    (home / ".config" / "recall" / "config.json").write_text('{"sources": []}')
    (home / ".config" / "brainstack" / "extractors.toml").write_text("[extractor]\n")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("BRAIN_ROOT", str(brain))

    return {
        "home": home,
        "brain": brain,
        "zshrc": zshrc,
        "claude_md": home / ".claude" / "CLAUDE.md",
        "codex_md": home / ".codex" / "AGENTS.md",
        "cursorrules": home / ".cursor" / ".cursorrules",
        "dream_plist": plist_dir / "com.user.agent-dream.plist",
        "sync_plist": plist_dir / "com.user.agent-sync.plist",
        "recall_symlink": recall_bin,
        "cache_dir": home / ".cache" / "recall",
        "recall_config": home / ".config" / "recall",
        "brainstack_config": home / ".config" / "brainstack",
        "important_lesson": brain / "memory" / "semantic" / "lessons" / "important.md",
    }


class TestUninstallScriptExists:
    def test_uninstall_sh_is_a_file(self):
        assert UNINSTALL_SH.is_file(), (
            "uninstall.sh must exist as a peer to install.sh — discoverable name"
        )

    def test_uninstall_sh_is_executable(self):
        assert os.access(UNINSTALL_SH, os.X_OK), "uninstall.sh must be executable"


class TestDryRun:
    def test_dry_run_lists_inventory_without_removing(self, fake_install):
        result = _run(UNINSTALL_SH, "--dry-run", "-y")
        out = result.stdout + result.stderr
        # The plan/inventory must be visible to the user
        assert "would" in out.lower() or "dry" in out.lower() or "plan" in out.lower(), (
            f"dry-run should announce itself as a plan; got:\n{out}"
        )
        # Mentions at least one specific surface
        assert "com.user.agent" in out or "launchd" in out.lower()

        # Nothing should have changed on disk
        for path in [
            fake_install["zshrc"],
            fake_install["claude_md"],
            fake_install["codex_md"],
            fake_install["cursorrules"],
            fake_install["dream_plist"],
            fake_install["sync_plist"],
            fake_install["recall_symlink"],
            fake_install["cache_dir"],
        ]:
            assert path.exists() or path.is_symlink(), (
                f"--dry-run touched {path}"
            )
        # Brain must survive dry-run obviously
        assert fake_install["important_lesson"].exists()


class TestDefaultUninstall:
    def test_removes_launchd_plists(self, fake_install):
        _run(UNINSTALL_SH, "-y")
        assert not fake_install["dream_plist"].exists()
        assert not fake_install["sync_plist"].exists()

    def test_strips_shell_init_block(self, fake_install):
        _run(UNINSTALL_SH, "-y")
        zshrc = fake_install["zshrc"].read_text()
        # User content survives
        assert "alias my-favorite=true" in zshrc
        assert "more user content below" in zshrc
        # Brainstack block stripped
        assert "brainstack-shell-banner" not in zshrc

    def test_removes_recall_first_blocks_from_host_configs(self, fake_install):
        _run(UNINSTALL_SH, "-y")
        for cfg in [fake_install["claude_md"], fake_install["codex_md"], fake_install["cursorrules"]]:
            content = cfg.read_text()
            assert "brainstack-recall-first" not in content, (
                f"recall-first block still in {cfg}"
            )
        # And user content outside the block is preserved
        assert "user's claude config" in fake_install["claude_md"].read_text()
        assert "user's codex config" in fake_install["codex_md"].read_text()
        assert "user's cursor rules" in fake_install["cursorrules"].read_text()

    def test_removes_path_symlinks(self, fake_install):
        _run(UNINSTALL_SH, "-y")
        assert not fake_install["recall_symlink"].exists()
        assert not fake_install["recall_symlink"].is_symlink()

    def test_removes_cache_dir(self, fake_install):
        """The cache (Qdrant + fastembed weights) is regeneratable — wipe it
        on uninstall to free disk space. No prompt needed."""
        _run(UNINSTALL_SH, "-y")
        assert not fake_install["cache_dir"].exists()

    def test_preserves_user_brain_data(self, fake_install):
        """The contract: --uninstall NEVER deletes ~/.agent."""
        _run(UNINSTALL_SH, "-y")
        assert fake_install["brain"].is_dir(), "default uninstall must not touch ~/.agent"
        assert fake_install["important_lesson"].exists()
        assert "IRREPLACEABLE_USER_CONTENT" in fake_install["important_lesson"].read_text()

    def test_preserves_user_configs(self, fake_install):
        """User configs (recall/, brainstack/) capture personalization —
        preserve so reinstalling doesn't lose them."""
        _run(UNINSTALL_SH, "-y")
        assert fake_install["recall_config"].is_dir()
        assert (fake_install["recall_config"] / "config.json").exists()
        assert fake_install["brainstack_config"].is_dir()
        assert (fake_install["brainstack_config"] / "extractors.toml").exists()

    def test_prints_preserved_data_summary(self, fake_install):
        """After uninstall, the user should see exactly what was kept and
        how to delete it manually if desired."""
        result = _run(UNINSTALL_SH, "-y")
        out = result.stdout + result.stderr
        # Must surface the brain path so the user can find their data
        assert ".agent" in out
        # Must reference --purge-data as the explicit-opt-in for complete wipe
        assert "purge-data" in out.lower()


class TestPurgeData:
    def test_purge_data_deletes_brain(self, fake_install):
        """--purge-data is the explicit-opt-in to delete user memory."""
        _run(UNINSTALL_SH, "-y", "--purge-data")
        assert not fake_install["brain"].exists(), (
            "--purge-data must remove ~/.agent"
        )

    def test_purge_data_deletes_user_configs(self, fake_install):
        _run(UNINSTALL_SH, "-y", "--purge-data")
        assert not fake_install["recall_config"].exists()
        assert not fake_install["brainstack_config"].exists()

    def test_purge_data_also_removes_host_surfaces(self, fake_install):
        """--purge-data is a SUPERSET of default uninstall: it removes
        everything default does, PLUS the user data."""
        _run(UNINSTALL_SH, "-y", "--purge-data")
        # All the default-removed surfaces are also gone
        assert not fake_install["dream_plist"].exists()
        assert not fake_install["recall_symlink"].exists()
        zshrc = fake_install["zshrc"].read_text()
        assert "brainstack-shell-banner" not in zshrc


class TestEquivalentEntryPoints:
    def test_install_sh_uninstall_flag_equivalent(self, fake_install):
        """`./install.sh --uninstall -y` must produce the same outcome as
        `./uninstall.sh -y` (the latter is just a wrapper around the former)."""
        # Run via install.sh
        _run(INSTALL_SH, "--uninstall", "-y")

        # Verify same end state as the default uninstall tests
        assert not fake_install["dream_plist"].exists()
        assert not fake_install["recall_symlink"].exists()
        zshrc = fake_install["zshrc"].read_text()
        assert "brainstack-shell-banner" not in zshrc
        assert fake_install["important_lesson"].exists()


class TestIdempotency:
    def test_re_running_after_uninstall_is_safe(self, fake_install):
        _run(UNINSTALL_SH, "-y")
        # Second run must succeed (everything already gone — no error)
        result = _run(UNINSTALL_SH, "-y")
        assert result.returncode == 0

    def test_partial_install_doesnt_error(self, fake_install):
        """If some surfaces are missing (user manually deleted some), the
        uninstaller should NOT fail — it should skip absent ones cleanly."""
        fake_install["dream_plist"].unlink()
        fake_install["cursorrules"].unlink()
        result = _run(UNINSTALL_SH, "-y")
        assert result.returncode == 0
        # Remaining surfaces still cleaned up
        assert not fake_install["sync_plist"].exists()
