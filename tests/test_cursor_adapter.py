"""Tests for the Cursor adapter (`agent/tools/cursor_adapter.py`).

PR-B of the multi-tool migration series. The Cursor adapter ingests
`~/.cursor/plans/*.plan.md` files (and, optionally, project-root
`.cursorrules` files) into the brain's `personal/notes/cursor/` dir
under the `cursor` namespace.

Contract:
  - Plans round-trip byte-for-byte (companion is the source verbatim)
  - Filename preserved (incl. `.plan.md` suffix and `<slug>_<hex>` shape)
  - Idempotent (re-run produces the same target tree)
  - dry_run=True writes nothing
  - Refuses to migrate when source has no .plan.md files
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "tools"))
sys.path.insert(0, str(REPO_ROOT / "agent" / "memory"))

from migrate_dispatcher import (  # noqa: E402
    Adapter,
    MigrationResult,
    NoAdapterError,
    detect_format,
    dispatch,
    get_adapter_for,
    register_adapter,
    registered_adapters,
    unregister_adapter,
)


# Sample Cursor plan with realistic frontmatter + body
SAMPLE_PLAN = """---
name: Auto-close containers on load
overview: Test plan for adapter validation
todos:
  - id: do-thing
    content: do the thing
    status: completed
---

# Auto-close Containers on Load

## Background

Plain markdown body. Real plans look like this — YAML frontmatter
followed by a markdown narrative.
"""


# --- Adapter registration --------------------------------------------


def test_cursor_adapter_registered_on_import():
    """The adapter registers itself when migrate_dispatcher loads."""
    assert "cursor-plans" in registered_adapters(), \
        f"cursor-plans adapter not registered; got {registered_adapters()}"
    assert get_adapter_for("cursor-plans") is not None


def test_cursor_adapter_supports_format():
    adapter = get_adapter_for("cursor-plans")
    assert adapter is not None
    assert adapter.supports("cursor-plans")
    assert not adapter.supports("claude-code-flat")
    assert not adapter.supports("codex-cli")


# --- Migration via dispatcher ----------------------------------------


def test_cursor_plans_dispatch_dry_run_counts(tmp_path):
    src = tmp_path / "plans"
    src.mkdir()
    (src / "auto-close_a8b3.plan.md").write_text(SAMPLE_PLAN)
    (src / "fix-bug_c4d2.plan.md").write_text(SAMPLE_PLAN.replace("Auto-close", "Fix bug"))

    result = dispatch(src=src, dst=tmp_path / "brain", dry_run=True)
    assert result.format == "cursor-plans"
    assert result.dry_run is True
    assert result.files_planned == 2
    # No writes during dry-run
    assert not (tmp_path / "brain" / "memory" / "personal" / "notes" / "cursor").exists()


def test_cursor_plans_dispatch_executes(tmp_path):
    src = tmp_path / "plans"
    src.mkdir()
    (src / "auto-close_a8b3.plan.md").write_text(SAMPLE_PLAN)
    (src / "fix-bug_c4d2.plan.md").write_text(SAMPLE_PLAN.replace("Auto-close", "Fix bug"))

    result = dispatch(src=src, dst=tmp_path / "brain", dry_run=False)
    assert result.format == "cursor-plans"
    assert result.dry_run is False
    assert result.files_written == 2
    # Plans land in personal/notes/cursor/
    cursor_dir = tmp_path / "brain" / "memory" / "personal" / "notes" / "cursor"
    assert (cursor_dir / "auto-close_a8b3.plan.md").exists()
    assert (cursor_dir / "fix-bug_c4d2.plan.md").exists()
    # Content is byte-for-byte identical to source
    assert (cursor_dir / "auto-close_a8b3.plan.md").read_text() == SAMPLE_PLAN


def test_cursor_plans_idempotent(tmp_path):
    """Running migrate twice on the same source produces the same target."""
    src = tmp_path / "plans"
    src.mkdir()
    (src / "x_001.plan.md").write_text(SAMPLE_PLAN)

    dispatch(src=src, dst=tmp_path / "brain", dry_run=False)
    first = (tmp_path / "brain" / "memory" / "personal" / "notes" / "cursor" / "x_001.plan.md").read_text()

    dispatch(src=src, dst=tmp_path / "brain", dry_run=False)
    second = (tmp_path / "brain" / "memory" / "personal" / "notes" / "cursor" / "x_001.plan.md").read_text()
    assert first == second


def test_cursor_plans_no_adapter_error_for_empty_plans(tmp_path):
    """A plans-shaped dir with no `.plan.md` files isn't classified as
    cursor-plans (detect_format returns 'empty'); dispatch refuses cleanly."""
    src = tmp_path / "empty"
    src.mkdir()

    with pytest.raises(NoAdapterError):
        dispatch(src=src, dst=tmp_path / "brain", dry_run=True)


def test_cursor_plans_namespace_default(tmp_path):
    """Without an explicit namespace option, lands under the default 'cursor'
    sub-path (not 'default') so PR-A's namespace contract is honored."""
    src = tmp_path / "plans"
    src.mkdir()
    (src / "x.plan.md").write_text(SAMPLE_PLAN)

    result = dispatch(src=src, dst=tmp_path / "brain", dry_run=False)
    # MigrationResult records the logical namespace
    assert result.namespace == "cursor"
    # Physical path includes the 'cursor' segment
    assert (tmp_path / "brain" / "memory" / "personal" / "notes" / "cursor" / "x.plan.md").exists()


def test_cursor_plans_tool_specific_counters(tmp_path):
    """Adapter records `plans_imported` under tool_specific (the per-adapter
    escape hatch) — sets the precedent for PR-C's `episodes_imported`."""
    src = tmp_path / "plans"
    src.mkdir()
    (src / "a.plan.md").write_text(SAMPLE_PLAN)
    (src / "b.plan.md").write_text(SAMPLE_PLAN)
    (src / "c.plan.md").write_text(SAMPLE_PLAN)

    result = dispatch(src=src, dst=tmp_path / "brain", dry_run=False)
    assert result.tool_specific.get("plans_imported") == 3


def test_cursor_plans_skips_symlinked_plans(tmp_path):
    """Defense: a `*.plan.md` symlink to a sensitive file must not be
    followed during migration."""
    src = tmp_path / "plans"
    src.mkdir()
    (src / "real.plan.md").write_text(SAMPLE_PLAN)
    sensitive = tmp_path / "secret.txt"
    sensitive.write_text("SECRET_TOKEN=abc\n")
    (src / "pwned.plan.md").symlink_to(sensitive)

    result = dispatch(src=src, dst=tmp_path / "brain", dry_run=False)
    cursor_dir = tmp_path / "brain" / "memory" / "personal" / "notes" / "cursor"
    assert (cursor_dir / "real.plan.md").exists()
    # Symlink not followed — pwned.plan.md not migrated
    assert not (cursor_dir / "pwned.plan.md").exists()
    # Defensive: the secret content isn't anywhere under target
    for p in cursor_dir.rglob("*.md"):
        assert "SECRET_TOKEN" not in p.read_text()


# --- Format detection still works ---


def test_cursor_plans_detection_with_real_filename(tmp_path):
    """Real cursor plans use the `<slug>_<hex>.plan.md` pattern. The
    detector must classify a dir of these as `cursor-plans`."""
    src = tmp_path / "plans"
    src.mkdir()
    (src / "auto-close_containers_on_load_c8bf9a67.plan.md").write_text(SAMPLE_PLAN)
    (src / "fix-other-thing_a1b2c3d4.plan.md").write_text(SAMPLE_PLAN)

    assert detect_format(src) == "cursor-plans"


def test_cursor_adapter_raises_on_partial_failure(tmp_path):
    """Per codex P2: a failed write must NOT silently report success.
    Make the target dir read-only so the per-file write raises OSError —
    the adapter must propagate, not swallow."""
    src = tmp_path / "plans"
    src.mkdir()
    (src / "x.plan.md").write_text(SAMPLE_PLAN)

    # Pre-create the target dir as a read-only file (not a dir) so
    # `target_dir.mkdir` succeeds the first call but `atomic_write_bytes`
    # to a child path fails.
    target_dir = tmp_path / "brain" / "memory" / "personal" / "notes" / "cursor"
    target_dir.mkdir(parents=True)
    target_dir.chmod(0o500)  # readable but not writable
    try:
        with pytest.raises(OSError):
            dispatch(src=src, dst=tmp_path / "brain", dry_run=False)
    finally:
        # Restore permissions so pytest can clean up.
        target_dir.chmod(0o700)


def test_install_sh_migrate_routes_cursor_through_dispatcher(tmp_path):
    """Per codex P1: the non-dry `--migrate <path>` path must route
    Cursor sources through the dispatcher (NOT migrate.py), and must
    NOT install the symlink-native swap (Cursor keeps writing to its
    own dir; the brain ingests a snapshot)."""
    import shutil
    src = tmp_path / "plans"
    src.mkdir()
    (src / "x.plan.md").write_text(SAMPLE_PLAN)
    brain = tmp_path / "brain"
    (brain / "memory").mkdir(parents=True)
    (brain / "tools").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "agent" / "tools" / "migrate.py", brain / "tools")
    shutil.copy(REPO_ROOT / "agent" / "tools" / "migrate_dispatcher.py", brain / "tools")
    shutil.copy(REPO_ROOT / "agent" / "tools" / "cursor_adapter.py", brain / "tools")
    shutil.copy(REPO_ROOT / "agent" / "memory" / "_atomic.py", brain / "memory")

    install_script = REPO_ROOT / "install.sh"
    env = os.environ.copy()
    env["BRAIN_ROOT"] = str(brain)
    if "PYTHON_BIN" not in env:
        for c in ("python3.13", "python3.12", "python3.11", "python3.10"):
            if shutil.which(c):
                env["PYTHON_BIN"] = c
                break

    result = subprocess.run(
        ["bash", str(install_script), "--migrate", str(src)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert result.returncode == 0, \
        f"install --migrate cursor failed:\n{result.stderr}\n{result.stdout}"

    # Plan landed in the cursor-adapter target path, NOT the Claude one
    cursor_target = brain / "memory" / "personal" / "notes" / "cursor" / "x.plan.md"
    claude_target = brain / "memory" / "personal" / "notes" / "x.plan.md"
    assert cursor_target.exists(), \
        f"cursor adapter target missing — install.sh routed to migrate.py instead?\n" \
        f"  Brain layout: {sorted((brain / 'memory').rglob('*.md'))}"
    assert not claude_target.exists(), \
        f"Cursor source was migrated as Claude misc — wrong adapter!"

    # Source dir is NOT a symlink (Cursor keeps writing there)
    assert src.is_dir() and not src.is_symlink(), \
        f"Cursor source should NOT be symlinked; got: {src} (symlink={src.is_symlink()})"
    assert (src / "x.plan.md").exists(), \
        f"Cursor source dir mutated unexpectedly"


@pytest.mark.skipif(
    not Path.home().joinpath(".cursor/plans").is_dir(),
    reason="user's ~/.cursor/plans/ doesn't exist on this machine",
)
def test_cursor_plans_real_data_dry_run():
    """Dry-run against the user's actual ~/.cursor/plans/."""
    src = Path.home() / ".cursor" / "plans"
    plans = list(src.glob("*.plan.md"))
    if not plans:
        pytest.skip("user has no .plan.md files in ~/.cursor/plans")

    import tempfile
    with tempfile.TemporaryDirectory(prefix="brainstack-cursor-realdata-") as tmp:
        dst = Path(tmp)
        result = dispatch(src=src, dst=dst, dry_run=True)
        assert result.format == "cursor-plans"
        assert result.files_planned == len(plans), \
            f"plan count mismatch: {result.files_planned} planned vs {len(plans)} on disk"
