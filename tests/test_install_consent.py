"""TDD-red: consent gating for the full default install.

The adoption audit's core finding: the default install edits
~/.claude/settings.json, three host config files, and LaunchAgents
WITHOUT an explicit yes - and the install.sh header still claims it
"never auto-edits user settings". The fix:

  - Non-TTY without --yes -> fall back to --minimal (safe by default for
    `curl | bash` and CI), with a notice that names --yes.
  - --yes is the explicit consent for the full install.
  - With consent, the full plan (settings.json + the three host files)
    is printed UPFRONT, before any action executes.
  - The stale "never auto-edits user settings" claim is removed.
  - The summary names the install root so users know where install.sh
    lives for later --setup-X invocations.

Subprocess-level integration tests against the real install.sh, isolated
via tmp HOME + BRAINSTACK_SKIP_LAUNCHCTL=1 + BRAINSTACK_SKIP_CLI_INSTALL=1
(same harness as test_install_hardening.py).
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


def _fresh_env(fake_home: Path) -> dict:
    fake_home.mkdir(parents=True, exist_ok=True)
    (fake_home / "Library" / "LaunchAgents").mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["BRAIN_ROOT"] = str(fake_home / ".agent")
    env["BRAINSTACK_SKIP_LAUNCHCTL"] = "1"
    env["BRAINSTACK_SKIP_CLI_INSTALL"] = "1"
    env["GIT_AUTHOR_NAME"] = "Consent"
    env["GIT_AUTHOR_EMAIL"] = "consent@test"
    env["GIT_COMMITTER_NAME"] = "Consent"
    env["GIT_COMMITTER_EMAIL"] = "consent@test"
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


def _seed_host_dirs(fake_home: Path) -> dict[str, Path]:
    """Pre-create the three host config dirs + files with known content so
    we can assert exactly what the installer did (or did not) write."""
    (fake_home / ".claude").mkdir(parents=True, exist_ok=True)
    (fake_home / ".codex").mkdir(parents=True, exist_ok=True)
    (fake_home / ".cursor").mkdir(parents=True, exist_ok=True)
    files = {
        "claude_md": fake_home / ".claude" / "CLAUDE.md",
        "codex_md": fake_home / ".codex" / "AGENTS.md",
        "cursorrules": fake_home / ".cursor" / ".cursorrules",
    }
    files["claude_md"].write_text("# user claude config\n")
    files["codex_md"].write_text("# user codex config\n")
    files["cursorrules"].write_text("# user cursor rules\n")
    return files


class TestNonTtyConsentFallback:
    def test_non_tty_without_yes_falls_back_to_minimal(self, tmp_path: Path):
        """Bare `install.sh` with no TTY and no --yes: NO host-config edits,
        and a notice telling the user the full install needs --yes."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)
        host_files = _seed_host_dirs(fake_home)
        settings = fake_home / ".claude" / "settings.json"
        settings.write_text('{"existing": true}\n')

        result = _run(env=env)  # bare invocation; stdin is DEVNULL (non-TTY)
        assert result.returncode == 0, (
            f"bare non-TTY install must succeed (as minimal):\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        # settings.json untouched
        assert json.loads(settings.read_text()) == {"existing": True}, (
            "non-TTY install without --yes modified ~/.claude/settings.json"
        )
        # No plists
        plists = list((fake_home / "Library" / "LaunchAgents").glob("*.plist"))
        assert plists == [], f"non-TTY install wrote LaunchAgents: {plists}"
        # No sentinel blocks in any host file
        for name, path in host_files.items():
            assert "brainstack-recall-first" not in path.read_text(), (
                f"non-TTY install without --yes wrote a sentinel block "
                f"into {name} ({path})"
            )
        # The minimal fallback still installs the brain itself
        assert (fake_home / ".agent").is_dir(), (
            "minimal fallback should still create the brain"
        )
        # Notice: we fell back to minimal, and --yes is the way to opt in
        out = result.stdout
        assert "minimal" in out.lower(), (
            f"fallback notice missing 'minimal':\n{out}"
        )
        assert "--yes" in out, (
            f"fallback notice must mention --yes for the full install:\n{out}"
        )


class TestYesConsentsToFullInstall:
    def test_yes_consents_to_full_install(self, tmp_path: Path):
        """--yes is explicit consent: sentinel blocks land in all three host
        files and the auto-recall TOML flag flips to true."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)
        host_files = _seed_host_dirs(fake_home)

        result = _run("--yes", env=env)
        assert result.returncode == 0, (
            f"--yes full install failed:\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

        # Sentinel blocks present in all three host files, with a real
        # directive between the markers (style of
        # test_recall_first_sentinel_block_written_with_directive)
        for name, path in host_files.items():
            content = path.read_text()
            assert "brainstack-recall-first-start" in content, (
                f"--yes install: sentinel start missing in {name}:\n{content[:800]}"
            )
            assert "brainstack-recall-first-end" in content, (
                f"--yes install: sentinel end missing in {name}:\n{content[:800]}"
            )
            m = re.search(
                r"brainstack-recall-first-start.*?brainstack-recall-first-end",
                content, re.DOTALL,
            )
            assert m is not None
            assert len(m.group(0)) > 100, (
                f"sentinel block in {name} suspiciously short:\n{m.group(0)}"
            )

        # Auto-recall TOML flag actually set to true (style of
        # test_auto_recall_toml_flag_actually_set_to_true)
        toml = fake_home / ".agent" / "runtime" / "pyproject.toml"
        assert toml.is_file(), (
            f"runtime/pyproject.toml not written:\n{result.stdout}"
        )
        assert re.search(r"enable_auto_recall\s*=\s*true", toml.read_text()), (
            f"enable_auto_recall flag not 'true' in:\n{toml.read_text()[:1500]}"
        )

    def test_full_install_prints_plan_upfront(self, tmp_path: Path):
        """With --yes, stdout must include a plan section that lists
        settings.json and the three host config files BEFORE the actions
        execute."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)
        _seed_host_dirs(fake_home)

        result = _run("--yes", env=env)
        assert result.returncode == 0, (
            f"--yes install failed:\n{result.stdout}\n{result.stderr}"
        )

        out = result.stdout
        plan_idx = out.lower().find("plan")
        assert plan_idx != -1, f"no plan section in --yes output:\n{out}"

        markers = ("settings.json", "CLAUDE.md", "AGENTS.md", ".cursorrules")
        for marker in markers:
            idx = out.find(marker)
            assert idx != -1, f"plan missing {marker!r}:\n{out}"
            assert idx > plan_idx, (
                f"{marker!r} appears before the plan heading "
                f"(idx {idx} < plan idx {plan_idx}):\n{out}"
            )

        # The plan markers must come BEFORE the first executed-action marker
        action_idx = out.find("✓")  # first checkmark = first action result
        if action_idx != -1:
            for marker in markers:
                assert out.find(marker) < action_idx, (
                    f"plan entry {marker!r} only appears after actions "
                    f"started executing - plan must be printed upfront:\n{out}"
                )

    def test_install_summary_mentions_install_root(self, tmp_path: Path):
        """After a full --yes install, the summary must name the install
        root (where install.sh lives) so users can run --setup-X later."""
        fake_home = tmp_path / "fakehome"
        env = _fresh_env(fake_home)
        _seed_host_dirs(fake_home)

        result = _run("--yes", env=env)
        assert result.returncode == 0, (
            f"--yes install failed:\n{result.stdout}\n{result.stderr}"
        )

        out = result.stdout
        assert "Install root:" in out, (
            f"summary missing 'Install root:' line:\n{out}"
        )
        assert str(REPO_ROOT) in out, (
            f"summary does not name the repo path {REPO_ROOT}:\n{out}"
        )


class TestStaleSafetyClaim:
    def test_install_sh_has_no_stale_safety_claim(self):
        """install.sh used to claim it 'never auto-edits user settings'.
        Since v0.6.0 the default install DOES edit user settings (that is
        the point of consent gating). The stale claim must be gone."""
        content = INSTALL_SH.read_text()
        assert "never auto-edits user settings" not in content, (
            "install.sh still contains the stale claim 'never auto-edits "
            "user settings' - it contradicts the consented default install"
        )
