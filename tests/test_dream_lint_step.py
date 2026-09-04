"""The nightly dream cycle runs `recall lint` over the brain.

Why here
--------
`recall lint --mark` is the only mechanism that demotes a stale memory,
and today nothing runs it on a schedule. Folding it into the dream cycle
makes staleness a background property of the brain instead of something
the user has to remember to check.

Three constraints make this delicate:

  1. **It must never break the dream cycle.** Consolidation, decay and the
     review queue matter more than lint. Any failure degrades to a
     `lint_error=` field in the summary line, never an exception.
  2. **The dream cycle's interpreter may not be able to import recall.**
     The launchd job runs the install-time `python3`, not the repo venv,
     so `import recall` can fail. `<brain>/.brainstack-repo-path` pins the
     install root for a subprocess fallback.
  3. **Wikilink resolution spans both trees.** A plan under `imports/`
     linking `[[a-lesson]]` in `memory/` is a LIVE link. Linting the two
     dirs with separately-computed key sets would report it as broken —
     a false positive, which is the one thing lint is designed to avoid.

`auto_dream._lint_step` does not exist yet; every test reaches it through
`_lint_step()` so this module still COLLECTS in the red phase.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "memory"))

import auto_dream  # noqa: E402


def _lint_step(brain_root):
    """Call the step under test, failing loudly if it is not wired up."""
    fn = getattr(auto_dream, "_lint_step", None)
    assert fn is not None, "auto_dream._lint_step is not implemented"
    return fn(str(brain_root))


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


ENTRY = {
    "id": "entry-1",
    "salience": 8,
    "summary": "Traced a flaky nightly job to a fixture teardown order.",
    "claim": "Release the shared port in teardown before the next test acquires it.",
}


@pytest.fixture
def brain(tmp_path: Path, monkeypatch) -> Path:
    """A brain with one stale memory under `memory/` and one under
    `imports/`, plus a live cross-tree wikilink that must NOT be flagged."""
    root = tmp_path / ".agent"
    for sub in ("memory/episodic/snapshots", "memory/working",
                "memory/candidates", "memory/semantic/lessons",
                "imports/claude/plans"):
        (root / sub).mkdir(parents=True, exist_ok=True)

    episodic = root / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl"
    episodic.write_text(json.dumps(ENTRY) + "\n", encoding="utf-8")

    # Stale #1: a lesson missing the required frontmatter fields.
    _write(root / "memory" / "semantic" / "lessons" / "broken-lesson.md",
           "---\ntype: lesson\n---\n\nA lesson with no name or description.\n")
    # Stale #2: an imported plan pointing at a memory that does not exist.
    _write(root / "imports" / "claude" / "plans" / "ghost-plan.md",
           "---\nname: ghost-plan\n---\n\nFollow up in [[ghost-target]].\n")
    # Healthy, and the target of the live cross-tree link below.
    _write(root / "memory" / "semantic" / "lessons" / "atomic-writes.md",
           "---\nname: atomic-writes\ndescription: Temp file plus rename.\n"
           "type: lesson\n---\n\nWrite to a sibling temp file, then rename.\n")
    _write(root / "imports" / "claude" / "plans" / "live-plan.md",
           "---\nname: live-plan\n---\n\nSee [[atomic-writes]] before shipping.\n")

    monkeypatch.setenv("BRAIN_ROOT", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    monkeypatch.delenv("BRAINSTACK_DREAM_LINT", raising=False)

    monkeypatch.setattr(auto_dream, "ROOT", str(root / "memory"))
    monkeypatch.setattr(auto_dream, "EPISODIC", str(episodic))
    monkeypatch.setattr(auto_dream, "EPISODIC_LOCK", str(episodic) + ".lock")
    monkeypatch.setattr(auto_dream, "CANDIDATES", str(root / "memory" / "candidates"))
    monkeypatch.setattr(auto_dream, "SEMANTIC", str(root / "memory" / "semantic"))
    monkeypatch.setattr(auto_dream, "REVIEW_QUEUE",
                        str(root / "memory" / "working" / "REVIEW_QUEUE.md"))
    return root


def _flag(path: Path) -> bool:
    return "needs_review: true" in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# In-process path
# ---------------------------------------------------------------------------

class TestInProcess:
    def test_marks_stale_memories_in_both_trees(self, brain):
        summary = _lint_step(brain)

        assert "lint_findings=2" in summary
        assert "lint_files=2" in summary
        assert "lint_marked=2" in summary
        assert "lint_cleared=0" in summary
        assert _flag(brain / "memory" / "semantic" / "lessons" / "broken-lesson.md")
        assert _flag(brain / "imports" / "claude" / "plans" / "ghost-plan.md")

    def test_live_cross_tree_wikilink_is_not_flagged(self, brain):
        """`imports/…/live-plan.md` links a lesson under `memory/`. Known
        keys are computed brain-wide, so this resolves."""
        _lint_step(brain)
        assert not _flag(brain / "imports" / "claude" / "plans" / "live-plan.md")
        assert not _flag(brain / "memory" / "semantic" / "lessons" / "atomic-writes.md")

    def test_ignores_files_outside_memory_and_imports(self, brain):
        stray = _write(brain / "notes" / "scratch.md",
                       "---\nname: scratch\n---\n\nSee [[also-missing]].\n")
        summary = _lint_step(brain)
        assert "lint_findings=2" in summary
        assert not _flag(stray)

    def test_auto_clears_a_now_fresh_memory(self, brain):
        fresh = _write(
            brain / "memory" / "semantic" / "lessons" / "was-stale.md",
            "---\nname: was-stale\ndescription: Nothing wrong with it now.\n"
            "type: lesson\nneeds_review: true\n---\n\nAll good.\n")
        summary = _lint_step(brain)
        assert "lint_cleared=1" in summary
        assert not _flag(fresh)

    def test_summary_starts_with_a_space(self, brain):
        """It is concatenated onto the `dream cycle:` line, so it owns its
        leading separator."""
        summary = _lint_step(brain)
        assert summary.startswith(" ")

    def test_disabled_by_env(self, brain, monkeypatch):
        monkeypatch.setenv("BRAINSTACK_DREAM_LINT", "0")
        assert _lint_step(brain) == ""
        assert not _flag(brain / "memory" / "semantic" / "lessons" / "broken-lesson.md")

    def test_never_raises_and_reports_the_error(self, brain, monkeypatch):
        import recall.lint

        def boom(*a, **k):
            raise RuntimeError("lint exploded")

        monkeypatch.setattr(recall.lint, "lint_dirs", boom, raising=False)
        summary = _lint_step(brain)
        assert "lint_error=" in summary
        assert "lint_findings=" not in summary


# ---------------------------------------------------------------------------
# Subprocess fallback (the launchd interpreter cannot import recall)
# ---------------------------------------------------------------------------

class TestSubprocessFallback:
    @staticmethod
    def _fake_install_root(tmp_path: Path, argv_log: Path, stdout: str) -> Path:
        root = tmp_path / "brainstack-clone"
        py = root / ".venv" / "bin" / "python"
        py.parent.mkdir(parents=True)
        py.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$@" > {argv_log}\n'
            "cat <<'JSON'\n" + stdout + "\nJSON\n",
            encoding="utf-8")
        py.chmod(0o755)
        return root

    def test_falls_back_to_the_pinned_repo_venv(self, brain, tmp_path, monkeypatch):
        argv_log = tmp_path / "argv.txt"
        findings = json.dumps([
            {"file": "a.md", "line": 1, "kind": "dead_path",
             "severity": "stale", "detail": "d", "evidence": "e"},
            {"file": "b.md", "line": 2, "kind": "broken_wikilink",
             "severity": "stale", "detail": "d", "evidence": "e"},
        ])
        root = self._fake_install_root(tmp_path, argv_log, findings)
        (brain / ".brainstack-repo-path").write_text(str(root) + "\n",
                                                     encoding="utf-8")
        # The launchd python cannot import recall.
        monkeypatch.setitem(sys.modules, "recall.lint", None)

        summary = _lint_step(brain)

        assert "lint_findings=2" in summary
        assert "lint_via=subprocess" in summary
        args = argv_log.read_text(encoding="utf-8").split()
        assert args[:5] == ["-m", "recall.cli", "lint", "--mark", "--json"]
        assert "--brain" in args
        assert str(brain) in args

    def test_missing_repo_path_pin_reports_an_error(self, brain, monkeypatch):
        monkeypatch.setitem(sys.modules, "recall.lint", None)
        summary = _lint_step(brain)
        assert "lint_error=" in summary

    def test_subprocess_failure_reports_an_error(self, brain, tmp_path, monkeypatch):
        root = tmp_path / "brainstack-clone"
        py = root / ".venv" / "bin" / "python"
        py.parent.mkdir(parents=True)
        py.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
        py.chmod(0o755)
        (brain / ".brainstack-repo-path").write_text(str(root), encoding="utf-8")
        monkeypatch.setitem(sys.modules, "recall.lint", None)

        summary = _lint_step(brain)
        assert "lint_error=" in summary
        assert "lint_findings=2" not in summary


# ---------------------------------------------------------------------------
# Wiring into the cycle
# ---------------------------------------------------------------------------

class TestDreamCycleWiring:
    @pytest.mark.timeout(120)
    def test_summary_fields_land_on_the_dream_cycle_line(self, brain, capsys):
        auto_dream.run_dream_cycle()
        out = capsys.readouterr().out
        assert "dream cycle:" in out
        line = next(ln for ln in out.splitlines() if ln.startswith("dream cycle:"))
        assert "lint_findings=" in line or "lint_error=" in line

    @pytest.mark.timeout(120)
    def test_env_disable_removes_the_fields_from_the_line(
            self, brain, capsys, monkeypatch):
        monkeypatch.setenv("BRAINSTACK_DREAM_LINT", "0")
        auto_dream.run_dream_cycle()
        line = next(ln for ln in capsys.readouterr().out.splitlines()
                    if ln.startswith("dream cycle:"))
        assert "lint_" not in line

    @pytest.mark.timeout(120)
    def test_run_returns_a_lint_summary(self, brain):
        result = auto_dream.run(brain_root=str(brain))
        assert "lint_summary" in result
        assert ("lint_findings=" in result["lint_summary"]
                or "lint_error=" in result["lint_summary"])

    @pytest.mark.timeout(120)
    def test_a_lint_crash_does_not_break_the_cycle(self, brain, capsys, monkeypatch):
        import recall.lint

        def boom(*a, **k):
            raise RuntimeError("lint exploded")

        monkeypatch.setattr(recall.lint, "lint_dirs", boom, raising=False)
        auto_dream.run_dream_cycle()
        out = capsys.readouterr().out
        assert "dream cycle:" in out
        assert "lint_error=" in out
        # The rest of the cycle still ran.
        assert os.path.exists(auto_dream.REVIEW_QUEUE)
