"""Pre-distribution: `install.sh` must leave a fresh machine with a
working `recall` command AND launchd plists with no `REPLACE_*` placeholders.

The audit caught:

  B2 — `install.sh` never calls `pip install -e '.[embeddings,mcp]'`. The
       symlink at `~/.local/bin/recall` points into `<repo>/.venv/bin/recall`,
       but that file only exists if pip installed the package into the
       venv. The installer creates the venv (line 2287-ish) but skips the
       editable install. After a colleague runs `./install.sh ...`,
       `recall query "..."` returns `command not found: recall`.

  B3 — `templates/com.user.agent-{dream,sync}.plist` literally contain
       `<string>REPLACE_HOME/...</string>` and `<string>REPLACE_PYTHON</string>`.
       The README + install.sh don't auto-expand them. A colleague who
       copies the plists as-is to `~/Library/LaunchAgents/` gets silent
       launchd failures — the hourly sync + nightly dream never fire.

Tests pin both as static guarantees (read install.sh + plist templates).
A full live install test is in tests/test_install_brain_remote.py.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestInstallShPipInstallsRecallPackage:
    """install.sh must install the brainstack package into the venv it
    creates — WITH the [embeddings,mcp] extras (otherwise `recall query`
    crashes on `import qdrant_client`). The actual pip-install logic
    lives in bin/install-recall-cli.sh which install.sh invokes."""

    def test_recall_cli_helper_installs_with_extras(self):
        """The CLI helper at bin/install-recall-cli.sh must `pip install -e`
        with the [embeddings,mcp] extras. A bare `pip install -e .` gets
        the CLI shim but no runtime deps."""
        helper = (REPO_ROOT / "bin" / "install-recall-cli.sh")
        assert helper.is_file(), f"missing helper: {helper}"
        content = helper.read_text()

        # Must include the extras spec — either inline or via env var
        has_extras_install = bool(
            re.search(r"pip\s+install[^\n]*-e[^\n]*\.\s*\[\s*embeddings", content)
            or re.search(r"\[embeddings,\s*mcp\]", content)
            or re.search(r"REPO_DIR\}\[embeddings", content)
        )
        assert has_extras_install, (
            "bin/install-recall-cli.sh must `pip install -e '.[embeddings,mcp]'` "
            "(not bare `.`) so qdrant_client + fastembed + mcp are available "
            "at import time. Without extras, `recall query` crashes."
        )

    def test_install_sh_invokes_the_cli_helper(self):
        """Sanity: install.sh's main flow calls the helper. Otherwise even
        a perfect helper does nothing."""
        install_sh = (REPO_ROOT / "install.sh").read_text()
        invokes_helper = "install-recall-cli.sh" in install_sh
        assert invokes_helper, (
            "install.sh must invoke bin/install-recall-cli.sh in its main "
            "flow. Otherwise the recall CLI is never set up."
        )


class TestSetupLaunchdEndToEnd:
    """End-to-end smoke: `./install.sh --setup-launchd` against a tmp HOME
    must write fully-expanded plists with NO REPLACE_* placeholders, and
    (with the safety env var) must NOT call launchctl against the live
    user-domain launchd."""

    def test_setup_launchd_expands_placeholders_and_writes_plists(
        self, tmp_path, monkeypatch
    ):
        import os
        import subprocess

        fake_home = tmp_path / "fakehome"
        (fake_home / "Library" / "LaunchAgents").mkdir(parents=True)
        (fake_home / ".agent").mkdir()

        env = os.environ.copy()
        env["HOME"] = str(fake_home)
        env["BRAIN_ROOT"] = str(fake_home / ".agent")
        env["BRAINSTACK_SKIP_LAUNCHCTL"] = "1"  # don't pollute live launchd

        result = subprocess.run(
            [str(REPO_ROOT / "install.sh"), "--setup-launchd"],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"setup-launchd failed:\nstdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )

        plist_dir = fake_home / "Library" / "LaunchAgents"
        for name in ("com.user.agent-dream.plist", "com.user.agent-sync.plist"):
            dest = plist_dir / name
            assert dest.is_file(), f"{name} not written"
            content = dest.read_text()
            assert "REPLACE_HOME" not in content, (
                f"{name} still has REPLACE_HOME placeholder"
            )
            assert "REPLACE_PYTHON" not in content, (
                f"{name} still has REPLACE_PYTHON placeholder"
            )
            # And the expanded values should refer to the tmp HOME
            assert str(fake_home) in content, (
                f"{name} should reference $HOME = {fake_home}, got:\n{content[:500]}"
            )


class TestPlistTemplatesHaveAutoExpansionMechanism:
    """The plist templates have REPLACE_HOME / REPLACE_PYTHON placeholders.
    install.sh MUST expand them when wiring launchd — or fail loudly if
    the user is expected to do it manually. Silent placeholder-as-plist
    causes launchd to silently fail."""

    def test_install_sh_handles_plist_placeholder_substitution(self):
        """We check for any of:
          (a) An sed/awk substitution of REPLACE_HOME or REPLACE_PYTHON
              within install.sh; OR
          (b) A `--setup-dream-agent` / `--setup-sync-agent` mode that
              writes an expanded plist; OR
          (c) A heredoc/print that contains the user's $HOME / $PYTHON_BIN
              filled in, instructing the user where to copy from.

        The point is that the user should NOT end up with REPLACE_* strings
        in their installed plist.
        """
        install_sh = (REPO_ROOT / "install.sh").read_text()
        # Look for the expansion logic — either sed of the placeholders,
        # or direct string-substitution in a Python heredoc.
        has_sed_expansion = bool(
            re.search(r"sed.*REPLACE_HOME", install_sh)
            or re.search(r"sed.*REPLACE_PYTHON", install_sh)
        )
        has_python_substitution = bool(
            re.search(r"REPLACE_HOME.*replace", install_sh)
            or re.search(r"replace.*REPLACE_HOME", install_sh)
        )
        # OR a launchd-setup mode that mentions both placeholders together
        has_setup_mode = (
            "setup-dream" in install_sh
            or "setup-sync-agent" in install_sh
            or "setup-launchd" in install_sh
        )
        assert has_sed_expansion or has_python_substitution or has_setup_mode, (
            "install.sh must auto-expand REPLACE_HOME / REPLACE_PYTHON in "
            "templates/com.user.agent-{dream,sync}.plist before copying to "
            "~/Library/LaunchAgents/. Without this, launchd silently fails "
            "to run the hourly sync + nightly dream cycle. Found no sed/"
            "substitution/setup-mode logic for the placeholders."
        )

    def test_plist_templates_still_use_placeholders(self):
        """Sanity check: the templates should KEEP their REPLACE_* markers
        so install.sh can substitute consistently. If a contributor
        accidentally hardcodes a real path, that's a regression."""
        for name in (
            "com.user.agent-dream.plist",
            "com.user.agent-sync.plist",
        ):
            path = REPO_ROOT / "templates" / name
            assert path.is_file(), f"missing template: {path}"
            content = path.read_text()
            assert "REPLACE_HOME" in content, (
                f"{name} should keep REPLACE_HOME placeholder for "
                "install.sh to expand"
            )
            assert "REPLACE_PYTHON" in content, (
                f"{name} should keep REPLACE_PYTHON placeholder for "
                "install.sh to expand"
            )
            # No hardcoded real-user path. Allow `/Users/yourname` as the
            # documented example placeholder; flag any other `/Users/<name>`
            # that would imply a leak from the maintainer's machine.
            real_user_paths = [
                line for line in content.splitlines()
                if "/Users/" in line and "/Users/yourname" not in line
            ]
            assert not real_user_paths, (
                f"{name} contains hardcoded /Users/<name> paths "
                f"(other than the documented `/Users/yourname` example):\n"
                + "\n".join(real_user_paths)
            )
