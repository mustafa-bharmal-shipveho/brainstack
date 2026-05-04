"""Tests for `agent/tools/check_freshness.py`.

This module exists because brainstack's `install.sh` seeds
`~/.agent/{tools,memory,harness}/` from the repo at install time, and
later `git pull` of the repo does NOT propagate updates to the brain.
Two real-world bugs this tool catches (both observed 2026-05-04):

  1. Brain installed before the auto-migrate feature shipped → missing
     `~/.agent/tools/auto_migrate_install.py` → `--setup-auto-migrate`
     dies with "tools/...py is missing".

  2. Brain installed before the namespace work shipped → stale
     `~/.agent/memory/auto_dream.py` (no `_ns_paths`) → dream cycle
     silently skips codex / claude-sessions episodes.

Both failures are silent without drift detection. The tests below verify
the report identifies missing / stale / extra files correctly and
exercise the runtime helper `warn_if_drift()` that dream_runner and
auto-migrate-all call to surface drift on every LaunchAgent tick.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "tools"))

import check_freshness  # noqa: E402


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def _make_synthetic_repo_and_brain(tmp_path: Path) -> tuple[Path, Path]:
    """Build a minimal fake brainstack repo + matching brain layout."""
    repo = tmp_path / "repo"
    brain = tmp_path / "brain"
    # Match the dirs check_freshness tracks
    _write(repo / "agent" / "tools" / "alpha.py", "print('alpha v1')\n")
    _write(repo / "agent" / "tools" / "beta.sh", "#!/bin/sh\necho beta v1\n")
    _write(repo / "agent" / "memory" / "core.py", "X = 1\n")
    _write(repo / "agent" / "harness" / "hook.py", "Y = 1\n")
    _write(repo / "install.sh", "#!/bin/sh\n")  # marker file for repo discovery

    # Mirror to brain (in-sync starting point)
    for sub in ("tools", "memory", "harness"):
        (brain / sub).mkdir(parents=True, exist_ok=True)
    (brain / "tools" / "alpha.py").write_text("print('alpha v1')\n")
    (brain / "tools" / "beta.sh").write_text("#!/bin/sh\necho beta v1\n")
    (brain / "memory" / "core.py").write_text("X = 1\n")
    (brain / "harness" / "hook.py").write_text("Y = 1\n")
    return repo, brain


# ---------- detect_drift -----------------------------------------------


class TestDetectDrift:
    def test_in_sync_brain(self, tmp_path: Path):
        repo, brain = _make_synthetic_repo_and_brain(tmp_path)
        report = check_freshness.detect_drift(repo, brain)
        assert report["in_sync"] is True
        assert report["missing"] == []
        assert report["stale"] == []
        assert report["extra"] == []

    def test_missing_file_in_brain(self, tmp_path: Path):
        repo, brain = _make_synthetic_repo_and_brain(tmp_path)
        # Add a new tool to repo only
        _write(repo / "agent" / "tools" / "gamma.py", "print('gamma')\n")
        report = check_freshness.detect_drift(repo, brain)
        assert report["in_sync"] is False
        assert "gamma.py" in report["missing"]
        # The other files are still in sync
        assert report["stale"] == []

    def test_stale_file_when_content_differs(self, tmp_path: Path):
        repo, brain = _make_synthetic_repo_and_brain(tmp_path)
        # Repo upgraded to v2; brain still has v1
        (repo / "agent" / "memory" / "core.py").write_text("X = 2  # v2\n")
        report = check_freshness.detect_drift(repo, brain)
        assert report["in_sync"] is False
        assert "core.py" in report["stale"]

    def test_extra_file_in_brain_tools_counts_as_drift(self, tmp_path: Path):
        """A tool deleted upstream but still in the brain IS drift —
        without this, the brain runs deleted framework code forever and
        the CLI silently exits 0 (Codex 2026-05-04 P2 fix)."""
        repo, brain = _make_synthetic_repo_and_brain(tmp_path)
        (brain / "tools" / "obsolete.py").write_text("# old tool\n")
        report = check_freshness.detect_drift(repo, brain)
        assert "obsolete.py" in report["extra"]
        assert report["in_sync"] is False
        assert "extra" in report["summary"]

    def test_extra_files_in_memory_are_ignored(self, tmp_path: Path):
        """memory/ holds user data subdirs (semantic, candidates, etc.)
        — extra files there must NOT be flagged. Only tools/ and harness/
        get the extra-file scrutiny."""
        repo, brain = _make_synthetic_repo_and_brain(tmp_path)
        # User data files that shouldn't trigger anything
        (brain / "memory" / "semantic" / "lessons").mkdir(parents=True)
        (brain / "memory" / "semantic" / "lessons" / "user_lesson.md").write_text("user lesson\n")
        # Even an extra .py in memory/ shouldn't be flagged as `extra`
        # — could be a future user-added module.
        (brain / "memory" / "user_helper.py").write_text("# user helper\n")
        report = check_freshness.detect_drift(repo, brain)
        assert "user_helper.py" not in report["extra"]
        assert "user_lesson.md" not in report["extra"]

    def test_user_local_files_ignored(self, tmp_path: Path):
        """`*.user.*` files are user-local helpers and must never be
        flagged as missing/stale/extra. install.sh's --upgrade path
        already excludes them via rsync; the drift check must mirror
        that policy."""
        repo, brain = _make_synthetic_repo_and_brain(tmp_path)
        (brain / "tools" / "my_helper.user.py").write_text("# my custom thing\n")
        # Even if a *.user.py exists in repo (unusual but legal),
        # don't track it
        (repo / "agent" / "tools" / "shared.user.py").write_text("# shared user helper\n")
        report = check_freshness.detect_drift(repo, brain)
        assert all(".user." not in m for m in report["missing"])
        assert all(".user." not in s for s in report["stale"])
        assert all(".user." not in e for e in report["extra"])

    def test_summary_string_contains_actionable_hint(self, tmp_path: Path):
        repo, brain = _make_synthetic_repo_and_brain(tmp_path)
        _write(repo / "agent" / "tools" / "gamma.py", "x")
        report = check_freshness.detect_drift(repo, brain)
        # The summary must tell the user what to do — without it the
        # warning is just noise.
        assert "install.sh --upgrade" in report["summary"]


# ---------- _default_repo_dir ------------------------------------------


class TestRepoDiscovery:
    def test_finds_repo_via_pin_file(self, tmp_path: Path, monkeypatch):
        repo, brain = _make_synthetic_repo_and_brain(tmp_path)
        # Simulate install.sh having written the pin
        (brain / ".brainstack-repo-path").write_text(str(repo) + "\n")
        # When the file's parent is NOT structured like the repo, we fall
        # back to the brain pin
        monkeypatch.setattr(check_freshness, "__file__",
                             str(brain / "tools" / "check_freshness.py"))
        found = check_freshness._default_repo_dir(brain)
        assert found == repo

    def test_returns_none_without_pin_or_repo_layout(self, tmp_path: Path, monkeypatch):
        brain = tmp_path / "brain-no-pin"
        brain.mkdir()
        monkeypatch.setattr(check_freshness, "__file__",
                             str(brain / "tools" / "check_freshness.py"))
        assert check_freshness._default_repo_dir(brain) is None

    def test_pin_with_invalid_path_returns_none(self, tmp_path: Path, monkeypatch):
        brain = tmp_path / "brain"
        brain.mkdir()
        (brain / ".brainstack-repo-path").write_text("/nonexistent/path\n")
        monkeypatch.setattr(check_freshness, "__file__",
                             str(brain / "tools" / "check_freshness.py"))
        assert check_freshness._default_repo_dir(brain) is None


# ---------- warn_if_drift (runtime entry-point helper) -----------------


class TestWarnIfDrift:
    def test_silent_on_in_sync(self, tmp_path: Path):
        repo, brain = _make_synthetic_repo_and_brain(tmp_path)
        buf = io.StringIO()
        result = check_freshness.warn_if_drift(brain_root=brain, repo_dir=repo,
                                                stream=buf)
        assert result is False
        assert buf.getvalue() == ""

    def test_one_line_warning_on_drift(self, tmp_path: Path):
        repo, brain = _make_synthetic_repo_and_brain(tmp_path)
        # Make something stale
        (repo / "agent" / "memory" / "core.py").write_text("X = 99  # upgraded\n")
        buf = io.StringIO()
        result = check_freshness.warn_if_drift(brain_root=brain, repo_dir=repo,
                                                stream=buf)
        assert result is True
        out = buf.getvalue()
        assert "drift detected" in out
        assert "install.sh --upgrade" in out
        # One line, terminated with \n
        assert out.count("\n") == 1

    def test_swallows_exceptions(self, tmp_path: Path):
        """The runtime caller (dream_runner, auto-migrate) must never be
        broken by a flaky drift check. Any exception must be swallowed
        and the helper must return False (no drift detected)."""
        # Pass nonexistent paths — detect_drift won't find files but
        # shouldn't raise; warn_if_drift should treat as no-warn.
        buf = io.StringIO()
        result = check_freshness.warn_if_drift(
            brain_root=tmp_path / "missing",
            repo_dir=tmp_path / "also-missing",
            stream=buf,
        )
        assert result is False
        assert buf.getvalue() == ""


# ---------- CLI smoke --------------------------------------------------


class TestCLI:
    def test_exit_code_0_on_in_sync(self, tmp_path: Path, capsys):
        repo, brain = _make_synthetic_repo_and_brain(tmp_path)
        rc = check_freshness.main(["--repo", str(repo), "--brain", str(brain)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "in sync" in out

    def test_exit_code_1_on_drift(self, tmp_path: Path, capsys):
        repo, brain = _make_synthetic_repo_and_brain(tmp_path)
        _write(repo / "agent" / "tools" / "newfile.py", "x")
        rc = check_freshness.main(["--repo", str(repo), "--brain", str(brain)])
        assert rc == 1
        out = capsys.readouterr().out
        assert "OUT OF SYNC" in out
        assert "newfile.py" in out
        assert "install.sh --upgrade" in out

    def test_quiet_mode_silent_on_in_sync(self, tmp_path: Path, capsys):
        repo, brain = _make_synthetic_repo_and_brain(tmp_path)
        rc = check_freshness.main([
            "--repo", str(repo), "--brain", str(brain), "--quiet",
        ])
        assert rc == 0
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_quiet_mode_emits_one_line_on_drift(self, tmp_path: Path, capsys):
        repo, brain = _make_synthetic_repo_and_brain(tmp_path)
        _write(repo / "agent" / "tools" / "x.py", "x")
        rc = check_freshness.main([
            "--repo", str(repo), "--brain", str(brain), "--quiet",
        ])
        assert rc == 1
        captured = capsys.readouterr()
        assert captured.out == ""  # quiet means no stdout
        assert "drift detected" in captured.err
        # One-line warning suitable for LaunchAgent log capture
        assert captured.err.count("\n") == 1

    def test_json_mode_returns_machine_parseable(self, tmp_path: Path, capsys):
        repo, brain = _make_synthetic_repo_and_brain(tmp_path)
        _write(repo / "agent" / "tools" / "x.py", "x")
        rc = check_freshness.main([
            "--repo", str(repo), "--brain", str(brain), "--json",
        ])
        assert rc == 1
        import json
        report = json.loads(capsys.readouterr().out)
        assert report["in_sync"] is False
        assert "x.py" in report["missing"]
