"""End-to-end tests for `install.sh --setup-recall-first-*` and its remove
counterparts.

Pattern follows the existing `tests/test_install_brain_remote.py` and the
pending-review installer tests: spawn `install.sh` in a tmp HOME, assert the
target file gained the brainstack-recall-first block between the sentinels,
then re-run to verify idempotency, then run the remove counterpart.

The setup mode targets three host files:
  - Claude Code:  ~/.claude/CLAUDE.md
  - Codex CLI:    ~/.codex/AGENTS.md
  - Cursor:       ~/.cursor/.cursorrules

The brainstack-managed block is wrapped in sentinels DISTINCT from the
pending-review installer's sentinels, so they coexist without colliding.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install.sh"

START = "<!-- brainstack-recall-first-start -->"
END = "<!-- brainstack-recall-first-end -->"


def _run_install(*args: str, env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [str(INSTALL_SH), *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Spin up an isolated HOME with the three host dirs pre-created.

    Each host dir starts with a minimal pre-existing config file so we can
    verify the installer preserves user content outside the sentinels.
    """
    home = tmp_path / "fakehome"
    (home / ".claude").mkdir(parents=True)
    (home / ".codex").mkdir(parents=True)
    (home / ".cursor").mkdir(parents=True)
    (home / ".agent").mkdir(parents=True)

    # Pre-existing user content the installer must NOT clobber.
    (home / ".claude" / "CLAUDE.md").write_text(
        "# User's Claude config\n\nDon't touch this section.\n"
    )
    (home / ".codex" / "AGENTS.md").write_text(
        "# User's Codex config\n\nDon't touch this section.\n"
    )
    (home / ".cursor" / ".cursorrules").write_text(
        "# User's Cursor rules\n\nDon't touch this section.\n"
    )

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("BRAIN_ROOT", str(home / ".agent"))
    return home


@pytest.fixture
def template_path() -> Path:
    p = REPO_ROOT / "templates" / "recall-first.md"
    assert p.is_file(), f"missing template: {p}"
    return p


class TestSetupRecallFirstClaude:
    def test_setup_adds_sentinel_block_to_claude_md(self, fake_home: Path, template_path: Path):
        result = _run_install("--setup-recall-first-claude")
        assert result.returncode == 0, f"stderr: {result.stderr}"

        claude_md = (fake_home / ".claude" / "CLAUDE.md").read_text()
        assert START in claude_md
        assert END in claude_md

    def test_setup_preserves_user_content(self, fake_home: Path):
        _run_install("--setup-recall-first-claude")
        claude_md = (fake_home / ".claude" / "CLAUDE.md").read_text()
        assert "# User's Claude config" in claude_md
        assert "Don't touch this section." in claude_md

    def test_setup_is_idempotent(self, fake_home: Path):
        r1 = _run_install("--setup-recall-first-claude")
        assert r1.returncode == 0
        content_after_first = (fake_home / ".claude" / "CLAUDE.md").read_text()

        r2 = _run_install("--setup-recall-first-claude")
        assert r2.returncode == 0
        content_after_second = (fake_home / ".claude" / "CLAUDE.md").read_text()

        assert content_after_first == content_after_second, (
            "re-running --setup-recall-first-claude must produce byte-identical output"
        )

        # Only one sentinel pair present
        assert content_after_second.count(START) == 1
        assert content_after_second.count(END) == 1

    def test_setup_coexists_with_pending_review_block(self, fake_home: Path):
        """A user who already has the pending-review block in CLAUDE.md
        should be able to add the recall-first block; both must coexist."""
        claude_md = fake_home / ".claude" / "CLAUDE.md"
        pre = (
            "# User's Claude config\n\n"
            "<!-- brainstack-pending-review-start -->\n"
            "@/path/to/PENDING_REVIEW.md\n"
            "<!-- brainstack-pending-review-end -->\n"
        )
        claude_md.write_text(pre)
        result = _run_install("--setup-recall-first-claude")
        assert result.returncode == 0

        after = claude_md.read_text()
        # Both blocks present
        assert "<!-- brainstack-pending-review-start -->" in after
        assert "<!-- brainstack-pending-review-end -->" in after
        assert START in after
        assert END in after

    def test_setup_skips_gracefully_if_claude_not_installed(
        self, fake_home: Path
    ):
        """If ~/.claude doesn't exist, exit 0 with a clear message — don't fail."""
        shutil.rmtree(fake_home / ".claude")
        result = _run_install("--setup-recall-first-claude")
        assert result.returncode == 0
        assert "not found" in (result.stdout + result.stderr).lower() or \
               "skip" in (result.stdout + result.stderr).lower()


class TestSetupRecallFirstCodex:
    def test_setup_adds_sentinel_block_to_agents_md(self, fake_home: Path):
        result = _run_install("--setup-recall-first-codex")
        assert result.returncode == 0, f"stderr: {result.stderr}"

        agents_md = (fake_home / ".codex" / "AGENTS.md").read_text()
        assert START in agents_md
        assert END in agents_md

    def test_setup_preserves_user_content(self, fake_home: Path):
        _run_install("--setup-recall-first-codex")
        agents_md = (fake_home / ".codex" / "AGENTS.md").read_text()
        assert "# User's Codex config" in agents_md

    def test_setup_is_idempotent(self, fake_home: Path):
        _run_install("--setup-recall-first-codex")
        first = (fake_home / ".codex" / "AGENTS.md").read_text()
        _run_install("--setup-recall-first-codex")
        second = (fake_home / ".codex" / "AGENTS.md").read_text()
        assert first == second


class TestSetupRecallFirstCursor:
    def test_setup_adds_sentinel_block_to_cursorrules(self, fake_home: Path):
        result = _run_install("--setup-recall-first-cursor")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        rules = (fake_home / ".cursor" / ".cursorrules").read_text()
        assert START in rules
        assert END in rules

    def test_setup_preserves_user_content(self, fake_home: Path):
        _run_install("--setup-recall-first-cursor")
        rules = (fake_home / ".cursor" / ".cursorrules").read_text()
        assert "# User's Cursor rules" in rules

    def test_setup_is_idempotent(self, fake_home: Path):
        _run_install("--setup-recall-first-cursor")
        first = (fake_home / ".cursor" / ".cursorrules").read_text()
        _run_install("--setup-recall-first-cursor")
        second = (fake_home / ".cursor" / ".cursorrules").read_text()
        assert first == second


class TestSetupRecallFirstAll:
    def test_setup_all_writes_all_three_files(self, fake_home: Path):
        result = _run_install("--setup-recall-first-all")
        assert result.returncode == 0, f"stderr: {result.stderr}"

        for target in [
            fake_home / ".claude" / "CLAUDE.md",
            fake_home / ".codex" / "AGENTS.md",
            fake_home / ".cursor" / ".cursorrules",
        ]:
            content = target.read_text()
            assert START in content, f"{target} missing recall-first block"

    def test_setup_all_with_partial_host_install(self, fake_home: Path):
        """If only Codex is installed, --setup-recall-first-all should write
        the Codex file and gracefully skip Claude / Cursor — not fail."""
        shutil.rmtree(fake_home / ".claude")
        shutil.rmtree(fake_home / ".cursor")

        result = _run_install("--setup-recall-first-all")
        assert result.returncode == 0
        assert START in (fake_home / ".codex" / "AGENTS.md").read_text()


class TestRemoveRecallFirst:
    def test_remove_strips_block_only(self, fake_home: Path):
        _run_install("--setup-recall-first-claude")
        result = _run_install("--remove-recall-first-claude")
        assert result.returncode == 0

        claude_md = (fake_home / ".claude" / "CLAUDE.md").read_text()
        assert START not in claude_md
        assert END not in claude_md
        # User content survives the removal
        assert "# User's Claude config" in claude_md

    def test_remove_when_block_absent_is_noop(self, fake_home: Path):
        """Calling --remove when no block exists must succeed and leave the
        file untouched."""
        before = (fake_home / ".claude" / "CLAUDE.md").read_text()
        result = _run_install("--remove-recall-first-claude")
        assert result.returncode == 0
        after = (fake_home / ".claude" / "CLAUDE.md").read_text()
        assert before == after

    def test_remove_all(self, fake_home: Path):
        _run_install("--setup-recall-first-all")
        result = _run_install("--remove-recall-first-all")
        assert result.returncode == 0
        for target in [
            fake_home / ".claude" / "CLAUDE.md",
            fake_home / ".codex" / "AGENTS.md",
            fake_home / ".cursor" / ".cursorrules",
        ]:
            assert START not in target.read_text(), (
                f"{target} still has recall-first block after --remove"
            )
