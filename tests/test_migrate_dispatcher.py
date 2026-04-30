"""Tests for agent/tools/migrate_dispatcher.py — discovery + adapter routing.

PR-A introduces a chassis for multi-tool migration. This file pins the
behavior:
  - `detect_format(src)` returns a tag for any source path
  - `discover_candidates(env)` walks known locations and returns Candidates
  - `dispatch(src, dst, ...)` routes to the right adapter, refusing if
    no adapter is registered for the detected format
  - Only ClaudeCodeAdapter is wired up in PR-A; cursor/codex sources are
    detected but refuse with a "PR-B/PR-C will handle this" message
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Add agent/tools to path so we can import migrate_dispatcher directly.
sys.path.insert(0, str(REPO_ROOT / "agent" / "tools"))
sys.path.insert(0, str(REPO_ROOT / "agent" / "memory"))

from migrate_dispatcher import (  # noqa: E402
    Adapter,
    AdapterRegistrationError,
    Candidate,
    MigrationResult,
    NoAdapterError,
    detect_format,
    discover_candidates,
    dispatch,
    get_adapter_for,
    register_adapter,
    registered_adapters,
    unregister_adapter,
)


# --- detect_format -----------------------------------------------------


def test_detect_format_claude_code_flat(tmp_path):
    """Root-level feedback_/user_/etc files → claude-code-flat."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "feedback_x.md").write_text("---\ntype: feedback\n---\nbody\n")
    (src / "user_alice.md").write_text("# alice\n")
    assert detect_format(src) == "claude-code-flat"


def test_detect_format_claude_code_nested(tmp_path):
    """Has personal/profile, personal/notes, etc → claude-code-nested."""
    src = tmp_path / "src"
    (src / "personal" / "profile").mkdir(parents=True)
    (src / "personal" / "profile" / "boss.md").write_text("# boss\n")
    (src / "semantic" / "lessons").mkdir(parents=True)
    (src / "semantic" / "lessons" / "feedback_x.md").write_text("body\n")
    assert detect_format(src) == "claude-code-nested"


def test_detect_format_claude_code_mixed(tmp_path):
    """Both flat and nested signals → claude-code-mixed."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "feedback_x.md").write_text("body\n")
    (src / "personal" / "profile").mkdir(parents=True)
    (src / "personal" / "profile" / "boss.md").write_text("# boss\n")
    assert detect_format(src) == "claude-code-mixed"


def test_detect_format_cursor_plans(tmp_path):
    """`*.plan.md` files (Cursor's plan dir) with no claude markers → cursor-plans."""
    src = tmp_path / "plans"
    src.mkdir()
    (src / "auto-close_containers_a8b3.plan.md").write_text("# Plan\n")
    (src / "fix-bug_c4d2.plan.md").write_text("# Plan\n")
    assert detect_format(src) == "cursor-plans"


def test_detect_format_codex_cli(tmp_path):
    """`sessions/<YYYY>/<MM>/<DD>/rollout-*.jsonl` → codex-cli."""
    src = tmp_path / "codex"
    sess = src / "sessions" / "2026" / "04" / "29"
    sess.mkdir(parents=True)
    (sess / "rollout-019dddd3-2c2b.jsonl").write_text('{"timestamp":"x"}\n')
    (src / "history.jsonl").write_text('{"cmd":"x"}\n')
    assert detect_format(src) == "codex-cli"


def test_detect_format_already_symlinked(tmp_path):
    """Source is a symlink → already-symlinked (regardless of target)."""
    brain = tmp_path / "brain" / "memory"
    brain.mkdir(parents=True)
    src = tmp_path / "claude_native"
    src.symlink_to(brain)
    assert detect_format(src) == "already-symlinked"


def test_detect_format_empty(tmp_path):
    """No .md / .jsonl files → empty."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "README.txt").write_text("hi")  # not migration material
    assert detect_format(src) == "empty"


def test_detect_format_unknown(tmp_path):
    """Has .md files but no recognized pattern → unknown."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "weird.md").write_text("# something\n")
    (src / "another.md").write_text("# also weird\n")
    # No feedback_/user_/etc prefix, no .plan.md suffix, no nested target dirs
    assert detect_format(src) == "unknown"


# --- discover_candidates ----------------------------------------------


def test_discover_candidates_finds_known_paths(tmp_path):
    """Synthesize a fake $HOME with claude/cursor/codex dirs; assert all
    are listed with the right format tags."""
    fake_home = tmp_path / "home"
    # Claude Code project memories
    (fake_home / ".claude" / "projects" / "-Users-foo-repo1" / "memory").mkdir(parents=True)
    (fake_home / ".claude" / "projects" / "-Users-foo-repo1" / "memory" / "feedback_x.md").write_text("body\n")
    (fake_home / ".claude" / "projects" / "-Users-foo-repo2" / "memory").mkdir(parents=True)
    (fake_home / ".claude" / "projects" / "-Users-foo-repo2" / "memory" / "user_alice.md").write_text("# alice\n")
    # Cursor plans
    (fake_home / ".cursor" / "plans").mkdir(parents=True)
    (fake_home / ".cursor" / "plans" / "do-thing_a1.plan.md").write_text("# plan\n")
    # Codex CLI sessions
    sess = fake_home / ".codex" / "sessions" / "2026" / "04" / "29"
    sess.mkdir(parents=True)
    (sess / "rollout-x.jsonl").write_text('{"x":1}\n')

    env = {"HOME": str(fake_home)}
    cands = discover_candidates(env=env)
    # Index by tagged format
    by_format: dict[str, list[Candidate]] = {}
    for c in cands:
        by_format.setdefault(c.format, []).append(c)

    # Both Claude project memories appear
    assert len(by_format.get("claude-code-flat", [])) == 2
    # Cursor plans dir appears
    assert len(by_format.get("cursor-plans", [])) == 1
    # Codex CLI sessions dir appears
    assert len(by_format.get("codex-cli", [])) == 1


def test_discover_candidates_handles_missing_paths_gracefully(tmp_path):
    """If $HOME has no AI tool dirs, discover returns empty list (not error)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    cands = discover_candidates(env={"HOME": str(fake_home)})
    assert cands == []


def test_discover_candidates_marks_already_symlinked(tmp_path):
    """A pre-symlinked claude project memory is reported as already-symlinked."""
    fake_home = tmp_path / "home"
    brain_memory = tmp_path / "brain" / "memory"
    brain_memory.mkdir(parents=True)
    proj = fake_home / ".claude" / "projects" / "-Users-foo-repo1"
    proj.mkdir(parents=True)
    (proj / "memory").symlink_to(brain_memory)

    env = {"HOME": str(fake_home), "BRAIN_ROOT": str(tmp_path / "brain")}
    cands = discover_candidates(env=env)
    statuses = [c.format for c in cands]
    assert "already-symlinked" in statuses


# --- dispatch ----------------------------------------------------------


def test_dispatch_routes_claude_code_to_adapter(tmp_path):
    """A claude-code-nested source goes through the existing migrate.py
    pipeline and produces a populated brain."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    (src / "feedback_x.md").write_text(
        "---\nname: x\ntype: feedback\n---\nbody\n\n"
        "**Why:** because\n\n**How to apply:** carefully\n"
    )
    result = dispatch(src=src, dst=dst, dry_run=False)
    assert result.format == "claude-code-flat"
    assert result.files_written >= 1
    assert (dst / "memory" / "semantic" / "lessons" / "feedback_x.md").exists()


def test_dispatch_routes_cursor_plans_to_adapter(tmp_path):
    """PR-B landed the Cursor adapter — cursor-plans sources now dispatch
    cleanly. (The pre-PR-B `test_dispatch_refuses_no_adapter_cursor` was
    a placeholder for this exact transition.)"""
    src = tmp_path / "plans"
    src.mkdir()
    (src / "thing_x.plan.md").write_text("# plan\n")

    result = dispatch(src=src, dst=tmp_path / "dst", dry_run=True)
    assert result.format == "cursor-plans"
    assert result.files_planned >= 1


def test_dispatch_refuses_no_adapter_codex(tmp_path):
    """A codex-cli source must refuse with a clear message until PR-C."""
    src = tmp_path / "codex"
    sess = src / "sessions" / "2026" / "04" / "29"
    sess.mkdir(parents=True)
    (sess / "rollout-x.jsonl").write_text('{"x":1}\n')

    with pytest.raises(NoAdapterError) as exc:
        dispatch(src=src, dst=tmp_path / "dst", dry_run=False)
    msg = str(exc.value).lower()
    assert "codex" in msg


def test_dispatch_dry_run_writes_nothing(tmp_path):
    """--dry-run produces a plan but writes 0 files under target."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    (src / "feedback_x.md").write_text(
        "---\ntype: feedback\n---\nbody\n\n**Why:** y\n\n**How to apply:** a\n"
    )
    result = dispatch(src=src, dst=dst, dry_run=True)
    assert result.dry_run is True
    # Files NOT actually written
    assert not (dst / "memory" / "semantic" / "lessons" / "feedback_x.md").exists()
    # But the plan was produced
    assert result.files_planned >= 1


def test_dispatch_dry_run_returns_zero_exit_for_supported_format(tmp_path):
    """Dry-run for a supported format completes cleanly (no exception)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "personal" / "profile").mkdir(parents=True)
    (src / "personal" / "profile" / "boss.md").write_text("# boss\n")

    result = dispatch(src=src, dst=tmp_path / "dst", dry_run=True)
    assert result.format == "claude-code-nested"
    assert result.files_planned >= 1


# --- install.sh integration ------------------------------------------


INSTALL_SCRIPT = REPO_ROOT / "install.sh"
MIGRATE_SCRIPT = REPO_ROOT / "agent" / "tools" / "migrate.py"
DISPATCHER_SCRIPT = REPO_ROOT / "agent" / "tools" / "migrate_dispatcher.py"
ATOMIC_SCRIPT = REPO_ROOT / "agent" / "memory" / "_atomic.py"


def _stage_brain(tmp_path: Path) -> Path:
    """Build a minimal brain layout for install.sh tests."""
    brain = tmp_path / "brain"
    (brain / "memory").mkdir(parents=True)
    (brain / "tools").mkdir(parents=True)
    shutil.copy(MIGRATE_SCRIPT, brain / "tools" / "migrate.py")
    shutil.copy(DISPATCHER_SCRIPT, brain / "tools" / "migrate_dispatcher.py")
    shutil.copy(ATOMIC_SCRIPT, brain / "memory" / "_atomic.py")
    return brain


def _python_bin_env(env: dict) -> dict:
    """Pick a Python ≥ 3.10 if the system one is too old (install.sh requires ≥3.10)."""
    if "PYTHON_BIN" not in env:
        for cand in ("python3.13", "python3.12", "python3.11", "python3.10"):
            if shutil.which(cand):
                env["PYTHON_BIN"] = cand
                break
    return env


def test_install_sh_dry_run_no_writes(tmp_path):
    """`./install.sh --migrate <path> --dry-run` must NOT touch the
    target dir at all."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "feedback_x.md").write_text(
        "---\ntype: feedback\n---\nbody\n\n**Why:** y\n\n**How to apply:** a\n"
    )
    brain = _stage_brain(tmp_path)
    env = _python_bin_env({**os.environ, "BRAIN_ROOT": str(brain)})

    def _user_files(root: Path) -> set[Path]:
        # Filter out Python's bytecode cache — __pycache__ is created
        # incidentally by `import _atomic` during planning and isn't a
        # write anyone would call "migration data".
        return {
            p for p in root.rglob("*")
            if "__pycache__" not in p.parts and not p.name.endswith(".pyc")
        }

    before_files = _user_files(brain / "memory")

    result = subprocess.run(
        ["bash", str(INSTALL_SCRIPT), "--migrate", str(src), "--dry-run"],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, f"dry-run failed:\n{result.stderr}\n{result.stdout}"

    # No new user-visible files in brain/memory
    after_files = _user_files(brain / "memory")
    assert before_files == after_files, \
        f"dry-run should not write files; new files: {after_files - before_files}"
    # And source still has its file
    assert (src / "feedback_x.md").exists()
    # Plan output mentions what would happen
    assert "would" in result.stdout.lower() or "dry" in result.stdout.lower() or \
        "plan" in result.stdout.lower()


def test_install_sh_interactive_lists_candidates(tmp_path):
    """`./install.sh --migrate` (no path) drops into discovery, prints
    candidates, and exits cleanly when user picks `none`."""
    fake_home = tmp_path / "home"
    (fake_home / ".claude" / "projects" / "-Users-foo-r1" / "memory").mkdir(parents=True)
    (fake_home / ".claude" / "projects" / "-Users-foo-r1" / "memory" / "feedback_a.md").write_text(
        "---\ntype: feedback\n---\nbody\n"
    )
    brain = _stage_brain(tmp_path)
    env = _python_bin_env({
        **os.environ,
        "HOME": str(fake_home),
        "BRAIN_ROOT": str(brain),
    })

    # Pipe "none" + newline as interactive selection
    result = subprocess.run(
        ["bash", str(INSTALL_SCRIPT), "--migrate"],
        input="none\n",
        capture_output=True, text=True, env=env,
        timeout=30,
    )
    # Exit 0 — user explicitly chose none
    assert result.returncode == 0, f"interactive --migrate failed:\n{result.stderr}\n{result.stdout}"
    # The discovered candidate's project slug should appear
    assert "Users-foo-r1" in result.stdout, \
        f"discovered claude project not listed:\n{result.stdout}"
    # The format tag should appear
    assert "claude-code" in result.stdout
    # No files written (we picked "none")
    assert not (brain / "memory" / "semantic").exists() or \
        not list((brain / "memory" / "semantic").rglob("*"))


# --- Fixes from Wave 5 review ----------------------------------------


def test_dispatch_refuses_when_dst_overlaps_src(tmp_path):
    """Brain-overlap guard at dispatcher level. Without it, dry-run could
    walk an already-migrated brain and report a misleading plan."""
    src = tmp_path / "brain"
    (src / "memory" / "personal" / "profile").mkdir(parents=True)
    (src / "memory" / "personal" / "profile" / "boss.md").write_text("# boss\n")

    # dst is a parent of src
    with pytest.raises(NoAdapterError) as exc:
        dispatch(src=src / "memory", dst=src, dry_run=True)
    assert "overlap" in str(exc.value).lower()


def test_dispatch_skips_symlinked_files_in_dry_run_plan(tmp_path):
    """`_plan` must skip symlinked .md files, mirroring migrate.py main()'s
    posture. Otherwise Python 3.10–3.12's symlink-following rglob could
    plan a migration of files outside the source tree."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "feedback_real.md").write_text(
        "---\ntype: feedback\n---\nbody\n"
    )
    sensitive = tmp_path / "sensitive.txt"
    sensitive.write_text("SECRET=abc\n")
    (src / "feedback_pwned.md").symlink_to(sensitive)

    result = dispatch(src=src, dst=tmp_path / "dst", dry_run=True)
    # Real feedback file IS in the plan
    assert result.files_planned == 1
    # Symlink was warned about, not silently included
    assert any("symlink" in w.lower() for w in result.warnings)


def test_migration_result_has_schema_and_serializes(tmp_path):
    """`MigrationResult` carries `schema_version` and `to_dict()` returns
    a stable shape — the contract PR-B/PR-C consumers will gate against."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "feedback_x.md").write_text("---\ntype: feedback\n---\nbody\n")
    result = dispatch(src=src, dst=tmp_path / "dst", dry_run=True)

    assert result.schema_version == 1
    d = result.to_dict()
    assert d["schema_version"] == 1
    assert d["format"] == "claude-code-flat"
    assert d["namespace"] == "default"
    assert d["dry_run"] is True
    assert "files_written" in d
    assert "files_planned" in d
    assert "tool_specific" in d
    # Round-trip via JSON to confirm the shape is JSON-clean
    json.dumps(d)


def test_migration_result_namespace_threads_through(tmp_path):
    """`options={'namespace': 'foo'}` lands on the result. PR-A wires the
    plumbing; PR-B/PR-C will route writes by namespace."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "feedback_x.md").write_text("---\ntype: feedback\n---\nbody\n")
    result = dispatch(
        src=src, dst=tmp_path / "dst", dry_run=True,
        options={"namespace": "experimental"},
    )
    assert result.namespace == "experimental"


# --- Adapter registry public API ----


class _FakeAdapter:
    """Minimal Adapter Protocol implementation for testing."""
    name = "fake"
    supported_formats = frozenset({"fake-format"})

    def supports(self, fmt: str) -> bool:
        return fmt in self.supported_formats

    def migrate(self, src, dst, dry_run, options=None):
        return MigrationResult(
            format="fake-format",
            files_written=42,
            files_planned=42,
            dry_run=dry_run,
        )


def test_register_adapter_and_get_adapter_for():
    """Public registry API: register, look up by format, unregister."""
    fake = _FakeAdapter()
    try:
        register_adapter(fake)
        assert "fake" in registered_adapters()
        assert get_adapter_for("fake-format") is fake
    finally:
        unregister_adapter("fake")
    assert "fake" not in registered_adapters()
    assert get_adapter_for("fake-format") is None


def test_register_adapter_rejects_duplicate_format():
    """Duplicate-format registration must fail fast — silent shadowing
    would be a debugging nightmare."""
    fake1 = _FakeAdapter()
    fake1.name = "fake1"

    class _OtherFake:
        name = "fake2"
        supported_formats = frozenset({"fake-format"})  # collides
        def supports(self, fmt):
            return fmt in self.supported_formats
        def migrate(self, src, dst, dry_run, options=None):
            raise NotImplementedError

    try:
        register_adapter(fake1)
        with pytest.raises(AdapterRegistrationError) as exc:
            register_adapter(_OtherFake())
        assert "already" in str(exc.value).lower() or "fake-format" in str(exc.value)
    finally:
        unregister_adapter("fake1")
        unregister_adapter("fake2")


def test_register_adapter_rejects_protocol_violation():
    """Adapter must implement the Protocol shape. Missing `migrate` is
    caught at register-time, not at dispatch-time."""
    class _BrokenAdapter:
        name = "broken"
        supported_formats = frozenset({"broken-format"})
        # Missing supports() and migrate()

    with pytest.raises(AdapterRegistrationError):
        register_adapter(_BrokenAdapter())


# --- Interactive flow correctness ----


def test_install_sh_interactive_picks_and_executes(tmp_path):
    """The KILLER bug: earlier `_interactive` plan-printed but never executed.
    This test pipes "1\\ny\\n" to actually pick a candidate and confirm —
    then asserts the brain has the migrated files AND the source dir was
    swapped to a symlink (codex review P1 #1)."""
    fake_home = tmp_path / "home"
    proj_mem = fake_home / ".claude" / "projects" / "-Users-foo-r1" / "memory"
    proj_mem.mkdir(parents=True)
    (proj_mem / "feedback_x.md").write_text(
        "---\ntype: feedback\n---\nA rule.\n\n**Why:** y\n\n**How to apply:** a\n"
    )
    brain = _stage_brain(tmp_path)
    env = _python_bin_env({
        **os.environ,
        "HOME": str(fake_home),
        "BRAIN_ROOT": str(brain),
    })

    # Pipe "1" (pick candidate 1) then "y" (confirm) → migration runs.
    result = subprocess.run(
        ["bash", str(INSTALL_SCRIPT), "--migrate"],
        input="1\ny\n",
        capture_output=True, text=True, env=env,
        timeout=60,
    )
    assert result.returncode == 0, \
        f"interactive happy path failed:\n{result.stderr}\n{result.stdout}"
    # Brain has the migrated companion file
    assert (brain / "memory" / "semantic" / "lessons" / "feedback_x.md").exists(), \
        f"interactive flow plan-printed but did not execute. " \
        f"Brain layout: {list((brain / 'memory').rglob('*'))}"
    # Source dir was swapped to a symlink to brain/memory — without this,
    # the user's future Claude Code writes would silently bypass the brain.
    assert proj_mem.is_symlink(), \
        f"interactive flow migrated files but did not install the native " \
        f"symlink. {proj_mem} should be a symlink to {brain}/memory."
    assert os.path.realpath(proj_mem) == os.path.realpath(brain / "memory"), \
        f"native symlink points at the wrong target: " \
        f"{os.path.realpath(proj_mem)} != {os.path.realpath(brain / 'memory')}"
    # Backup of original source content exists
    backups = list(proj_mem.parent.glob(f"{proj_mem.name}.bak.*"))
    assert backups, f"expected a timestamped backup; got {list(proj_mem.parent.iterdir())}"


def test_install_sh_interactive_rejects_zero_choice(tmp_path):
    """`"0"` must be rejected — Python's negative indexing would silently
    pick the last candidate. Per reliability persona HIGH #6."""
    fake_home = tmp_path / "home"
    (fake_home / ".claude" / "projects" / "-Users-foo-r1" / "memory").mkdir(parents=True)
    (fake_home / ".claude" / "projects" / "-Users-foo-r1" / "memory" / "feedback_x.md").write_text(
        "---\ntype: feedback\n---\nbody\n"
    )
    brain = _stage_brain(tmp_path)
    env = _python_bin_env({
        **os.environ,
        "HOME": str(fake_home),
        "BRAIN_ROOT": str(brain),
    })

    result = subprocess.run(
        ["bash", str(INSTALL_SCRIPT), "--migrate"],
        input="0\n",
        capture_output=True, text=True, env=env,
        timeout=30,
    )
    # Should NOT exit 0 — refusal exit
    assert result.returncode != 0, \
        f"choice '0' should be rejected, exited {result.returncode}"
    assert "out of range" in result.stdout.lower() or "out of range" in result.stderr.lower()
    # No files written
    assert not (brain / "memory" / "semantic").exists() or \
        not list((brain / "memory" / "semantic").rglob("*.md"))


def test_install_sh_interactive_warns_on_multi_claude(tmp_path):
    """When 2+ Claude memories detected, print collision warning."""
    fake_home = tmp_path / "home"
    for slug in ("-Users-foo-r1", "-Users-foo-r2", "-Users-foo-r3"):
        mem = fake_home / ".claude" / "projects" / slug / "memory"
        mem.mkdir(parents=True)
        (mem / "feedback_a.md").write_text("---\ntype: feedback\n---\nbody\n")

    brain = _stage_brain(tmp_path)
    env = _python_bin_env({
        **os.environ,
        "HOME": str(fake_home),
        "BRAIN_ROOT": str(brain),
    })

    result = subprocess.run(
        ["bash", str(INSTALL_SCRIPT), "--migrate"],
        input="none\n",
        capture_output=True, text=True, env=env,
        timeout=30,
    )
    assert result.returncode == 0
    # Warning text shows up
    assert "warning" in result.stdout.lower() and \
        ("namespace" in result.stdout.lower() or "overwrite" in result.stdout.lower()), \
        f"multi-Claude warning missing:\n{result.stdout}"


def test_dispatch_handles_already_symlinked_source_inside_brain(tmp_path):
    """Codex P2 #2: an already-symlinked source like
    `~/.claude/.../memory -> $BRAIN_ROOT/memory` previously failed the
    new overlap check (because src.resolve() lands inside dst.resolve()).
    The fix detects 'already-symlinked' BEFORE running the overlap guard."""
    brain = tmp_path / "brain"
    (brain / "memory" / "personal" / "profile").mkdir(parents=True)
    (brain / "memory" / "personal" / "profile" / "boss.md").write_text("# boss\n")

    # User's project memory is a symlink to brain/memory
    proj = tmp_path / "fake_home" / ".claude" / "projects" / "-Users-x" / "memory"
    proj.parent.mkdir(parents=True)
    proj.symlink_to(brain / "memory")

    # dst is brain — src.resolve() == dst/memory which is INSIDE dst
    result = dispatch(src=proj, dst=brain, dry_run=True)
    assert result.format == "already-symlinked"
    assert "symlink" in " ".join(result.warnings).lower()


def test_detect_format_claude_code_with_stray_plan_md(tmp_path):
    """Codex P2 #3: a Claude memory dir with `MEMORY.md` plus an
    accidentally-named `*.plan.md` file must NOT be classified as cursor-plans."""
    src = tmp_path / "claude_mem"
    src.mkdir()
    (src / "MEMORY.md").write_text("# Memory Index\n")
    (src / "feedback_x.md").write_text("---\ntype: feedback\n---\nbody\n")
    (src / "side_note.plan.md").write_text("# stray plan\n")  # decoy

    # Should be classified as Claude (MEMORY.md is the strong signal),
    # not cursor-plans (which would be refused by the dispatcher).
    assert detect_format(src).startswith("claude-code")


def test_detect_format_claude_code_bare_plus_memory_md(tmp_path):
    """Pin the `MEMORY.md + bare-named .md` heuristic from earlier work —
    real Claude Code memories in the wild have files like `slack_voice.md`
    without any prefix, alongside `MEMORY.md`. They must classify as
    `claude-code-flat`, not `unknown`."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "MEMORY.md").write_text("# index\n")
    (src / "slack_voice.md").write_text("# slack voice\n")
    (src / "team_ownership.md").write_text("# ownership\n")
    assert detect_format(src) == "claude-code-flat"


# --- Real-data sanity (existing) ----


@pytest.mark.skipif(
    not Path.home().joinpath(".claude/projects").is_dir(),
    reason="user's ~/.claude/projects/ doesn't exist on this machine",
)
def test_real_data_dry_run_all_claude_dirs():
    """For each non-symlinked ~/.claude/projects/<slug>/memory dir on the
    real machine, `dispatch(... dry_run=True)` produces a clean plan
    with files_planned > 0 and no exceptions. This is the confidence
    booster from PR #6's retro."""
    projects_dir = Path.home() / ".claude" / "projects"
    real_sources = []
    for p in projects_dir.iterdir():
        mem = p / "memory"
        if mem.is_symlink():
            continue
        if not mem.is_dir():
            continue
        if not any(mem.rglob("*.md")):
            continue
        real_sources.append(mem)

    if not real_sources:
        pytest.skip("no non-symlinked claude project memory dirs found")

    failures: list[str] = []
    for src in real_sources:
        # Use a sandbox dst — never touch ~/.agent
        import tempfile
        with tempfile.TemporaryDirectory(prefix="brainstack-realdata-") as tmp:
            dst = Path(tmp)
            try:
                result = dispatch(src=src, dst=dst, dry_run=True)
            except Exception as e:
                failures.append(f"{src}: {type(e).__name__}: {e}")
                continue
            if result.format == "unknown":
                failures.append(f"{src}: classified as unknown")
                continue
            if result.files_planned == 0 and result.format != "empty":
                failures.append(f"{src}: 0 files planned despite format={result.format}")

    assert not failures, "real-data dry-run failures:\n" + "\n".join(failures)
