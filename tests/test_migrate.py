"""Tests for tools/migrate.py — flat-memory-dir → 4-layer brain importer.

Migrate must:
  - parse `feedback_*.md` files into lessons.jsonl rows with rule + why +
    how_to_apply preserved (extension fields)
  - write companion long-form markdown for each migrated feedback file
  - route `user_*.md` → personal/profile/
  - route `project_*.md`, `cycle-*.md`, misc → personal/notes/
  - route `reference_*.md` → personal/references/
  - rewrite MEMORY.md index with new paths
  - be idempotent (re-running on the same input produces the same output)
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATE_SCRIPT = REPO_ROOT / "agent" / "tools" / "migrate.py"


def make_flat_memory(root: Path, files: dict[str, str]) -> None:
    """Write `files` (name → content) into `root`."""
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (root / name).write_text(content)


def run_migrate(source_dir: Path, target_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(MIGRATE_SCRIPT), str(source_dir), str(target_dir)],
        capture_output=True,
        text=True,
    )


# Sample feedback file with the user's actual structure
SAMPLE_FEEDBACK = """---
name: CI parity for tests
description: Always run the exact CI test command locally, not just npm test
type: feedback
---

Always identify and run the exact test command that CI uses (e.g., `jest --selectProjects unit`) rather than just `npm test`.

**Why:** PR #39 passed `npm test` locally but CI failed because CI runs `jest --selectProjects unit`.

**How to apply:** In Phase 0 of the agent team workflow, read `.circleci/config.yml` to find the exact test command.
"""

SAMPLE_USER = """---
name: User profile note
description: Notes about the user
type: user
---

This is profile information.
"""

SAMPLE_PROJECT = """---
name: Project foo
description: Active work on foo
type: project
---

Project context goes here.
"""

SAMPLE_REFERENCE = """---
name: Slack channel for X
description: Where bugs are tracked
type: reference
---

Channel: #bugs-x
"""


def test_feedback_becomes_lesson_with_extension_fields(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    make_flat_memory(src, {"feedback_ci_parity.md": SAMPLE_FEEDBACK})

    result = run_migrate(src, dst)
    assert result.returncode == 0, f"migrate failed: {result.stderr}"

    lessons_path = dst / "memory" / "semantic" / "lessons.jsonl"
    assert lessons_path.exists(), "lessons.jsonl was not created"
    lines = lessons_path.read_text().strip().splitlines()
    assert len(lines) == 1, "expected exactly one lesson row"
    lesson = json.loads(lines[0])

    assert "Always identify and run the exact test command" in lesson["claim"]
    assert lesson["status"] == "accepted"
    assert lesson["reviewer"] == "migrate.py"
    assert "PR #39" in lesson["why"], "why field not preserved"
    assert "Phase 0" in lesson["how_to_apply"], "how_to_apply field not preserved"
    assert "feedback_ci_parity" in lesson["original_markdown_path"]


def test_feedback_companion_markdown_written(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    make_flat_memory(src, {"feedback_ci_parity.md": SAMPLE_FEEDBACK})

    run_migrate(src, dst)

    companion_dir = dst / "memory" / "semantic" / "lessons"
    companions = list(companion_dir.glob("*.md"))
    assert len(companions) == 1, "expected one companion markdown"
    content = companions[0].read_text()
    # Companion preserves the original file content
    assert "**Why:**" in content
    assert "**How to apply:**" in content


def test_user_file_routed_to_personal_profile(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    make_flat_memory(src, {"user_alice.md": SAMPLE_USER})

    run_migrate(src, dst)

    profile = dst / "memory" / "personal" / "profile" / "alice.md"
    assert profile.exists(), "user_*.md should land in personal/profile/"
    assert "profile information" in profile.read_text()


def test_project_file_routed_to_personal_notes(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    make_flat_memory(src, {"project_foo.md": SAMPLE_PROJECT})

    run_migrate(src, dst)

    note = dst / "memory" / "personal" / "notes" / "project_foo.md"
    assert note.exists(), f"expected {note}"
    assert "Project context goes here" in note.read_text()


def test_cycle_file_routed_to_personal_notes(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    make_flat_memory(src, {"cycle-2026-04-26-foo.md": "# Cycle notes\n"})

    run_migrate(src, dst)

    note = dst / "memory" / "personal" / "notes" / "cycle-2026-04-26-foo.md"
    assert note.exists()


def test_reference_file_routed_to_personal_references(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    make_flat_memory(src, {"reference_slack_channel.md": SAMPLE_REFERENCE})

    run_migrate(src, dst)

    ref = dst / "memory" / "personal" / "references" / "slack_channel.md"
    assert ref.exists(), "reference_*.md should land in personal/references/"
    assert "#bugs-x" in ref.read_text()


def test_misc_file_routed_to_personal_notes(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    make_flat_memory(src, {"some-misc-doc.md": "# Misc\n"})

    run_migrate(src, dst)

    note = dst / "memory" / "personal" / "notes" / "some-misc-doc.md"
    assert note.exists()


def test_memory_md_index_rewritten(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    make_flat_memory(src, {
        "feedback_ci_parity.md": SAMPLE_FEEDBACK,
        "user_alice.md": SAMPLE_USER,
        "MEMORY.md": "# Memory Index\n\n- old line\n",
    })

    run_migrate(src, dst)

    new_index = dst / "memory" / "MEMORY.md"
    assert new_index.exists()
    content = new_index.read_text()
    # Index points at NEW locations, not old
    assert "personal/profile/alice.md" in content
    # Index is regenerated, not just copied
    assert "old line" not in content


def test_migration_is_idempotent(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    make_flat_memory(src, {"feedback_ci_parity.md": SAMPLE_FEEDBACK})

    run_migrate(src, dst)
    first_lessons = (dst / "memory" / "semantic" / "lessons.jsonl").read_text()

    run_migrate(src, dst)
    second_lessons = (dst / "memory" / "semantic" / "lessons.jsonl").read_text()

    assert first_lessons == second_lessons, "re-running migrate should be idempotent"


def test_full_user_dir_migration(tmp_path):
    """Mix of all file types lands in correct subdirs and produces a clean index."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    make_flat_memory(src, {
        "feedback_a.md": SAMPLE_FEEDBACK,
        "feedback_b.md": SAMPLE_FEEDBACK.replace("CI parity", "Other rule"),
        "user_x.md": SAMPLE_USER,
        "project_p.md": SAMPLE_PROJECT,
        "cycle-2026-04-26.md": "# cycle\n",
        "reference_r.md": SAMPLE_REFERENCE,
        "misc.md": "# misc\n",
    })

    result = run_migrate(src, dst)
    assert result.returncode == 0

    # Counts
    assert len(list((dst / "memory" / "semantic" / "lessons").glob("*.md"))) == 2
    lessons = (dst / "memory" / "semantic" / "lessons.jsonl").read_text().strip().splitlines()
    assert len(lessons) == 2
    assert (dst / "memory" / "personal" / "profile" / "x.md").exists()
    assert (dst / "memory" / "personal" / "notes" / "project_p.md").exists()
    assert (dst / "memory" / "personal" / "notes" / "cycle-2026-04-26.md").exists()
    assert (dst / "memory" / "personal" / "references" / "r.md").exists()
    assert (dst / "memory" / "personal" / "notes" / "misc.md").exists()
