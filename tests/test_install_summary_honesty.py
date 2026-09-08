"""The install summary must not claim a host surface it did not touch.

On a machine without Claude Code, Codex CLI or Cursor, the auto-recall and
recall-first defaults have nothing to wire: the sub-steps correctly print
"not found; skipping" — and then the summary printed "✓ Claude Code
auto-recall: done" and "✓ Recall-first directive: done" anyway (2026-09-08
full-day QA in an empty sandbox HOME). A user reading that believes the hook
is installed. The summary line must say skipped, name what was missing, and
flip to ✓ done only when the surface exists.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"


def _env(fake_home: Path) -> dict[str, str]:
    fake_home.mkdir(parents=True, exist_ok=True)
    (fake_home / "Library" / "LaunchAgents").mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({
        "HOME": str(fake_home),
        "BRAIN_ROOT": str(fake_home / ".agent"),
        "BRAINSTACK_SKIP_LAUNCHCTL": "1",
        "BRAINSTACK_SKIP_CLI_INSTALL": "1",
        "GIT_AUTHOR_NAME": "Summary Test", "GIT_AUTHOR_EMAIL": "summary@test",
        "GIT_COMMITTER_NAME": "Summary Test", "GIT_COMMITTER_EMAIL": "summary@test",
    })
    return env


def _install(env: dict[str, str]) -> str:
    r = subprocess.run(
        [str(INSTALL_SH), "--brain-remote", "git@example.com:test/scratch.git", "--yes"],
        env=env, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300,
    )
    assert r.returncode == 0, f"rc={r.returncode}\n{r.stdout[-3000:]}\n{r.stderr[-2000:]}"
    return r.stdout + r.stderr


def _summary_line(combined: str, opt_out_flag: str) -> str:
    m = re.search(rf"^\s*[✓•✗?]\s.+\({re.escape(opt_out_flag)}\)\s*$", combined, re.M)
    assert m, f"no summary line for {opt_out_flag}:\n{combined[-3000:]}"
    return m.group(0).strip()


@pytest.mark.skipif(not INSTALL_SH.exists(), reason="install.sh missing")
def test_no_host_tools_means_skipped_not_done(tmp_path: Path):
    combined = _install(_env(tmp_path / "home"))

    auto = _summary_line(combined, "--no-auto-recall")
    first = _summary_line(combined, "--no-recall-first")

    assert auto.startswith("•"), auto
    assert "skipped" in auto and "done" not in auto, auto
    assert ".claude" in auto or "Claude Code" in auto, auto
    assert first.startswith("•"), first
    assert "skipped" in first and "done" not in first, first
    # And no hook file was conjured into existence to make it "done".
    assert not (tmp_path / "home" / ".claude").exists()

    # The sub-step says the same thing on its own, instead of "Done ... on all
    # installed hosts" over three skips.
    r = subprocess.run(
        [str(INSTALL_SH), "--setup-recall-first-all"],
        env=_env(tmp_path / "home"), cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "nothing wired" in r.stdout.lower(), r.stdout
    assert "first-line resource on all installed hosts" not in r.stdout, r.stdout


@pytest.mark.skipif(not INSTALL_SH.exists(), reason="install.sh missing")
def test_with_claude_code_present_the_hook_is_wired_and_reported_done(tmp_path: Path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    combined = _install(_env(home))

    auto = _summary_line(combined, "--no-auto-recall")
    first = _summary_line(combined, "--no-recall-first")

    assert auto.startswith("✓") and "done" in auto, auto
    assert first.startswith("✓") and "done" in first, first
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert "brainstack-runtime" in json.dumps(settings)
    assert "recall" in (home / ".claude" / "CLAUDE.md").read_text().lower()
