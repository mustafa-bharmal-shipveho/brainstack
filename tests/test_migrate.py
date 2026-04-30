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


# ---------- Lossless-migration tests (gaps fix) ----------


def test_recursive_walk_preserves_nested_layout(tmp_path):
    """Modern Claude auto-memory uses nested dirs (personal/profile/*, etc.).
    Source files at those nested paths must land at the same relative paths
    under target memory/, not get silently dropped by a shallow iterdir."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"

    # Nested source layout (already-target-shaped)
    (src / "personal" / "profile").mkdir(parents=True)
    (src / "personal" / "profile" / "boss.md").write_text("# Boss profile\nNotes about boss.\n")

    (src / "personal" / "notes").mkdir(parents=True)
    (src / "personal" / "notes" / "foo.md").write_text("# Foo note\nSome project context.\n")

    (src / "personal" / "references").mkdir(parents=True)
    (src / "personal" / "references" / "slack.md").write_text("# Slack ref\n#channel-x\n")

    (src / "semantic" / "lessons").mkdir(parents=True)
    (src / "semantic" / "lessons" / "feedback_zzz.md").write_text(SAMPLE_FEEDBACK)

    result = run_migrate(src, dst)
    assert result.returncode == 0, f"migrate failed: {result.stderr}"

    # Each nested source file lands at the SAME relative path under target memory/
    assert (dst / "memory" / "personal" / "profile" / "boss.md").exists(), \
        "nested personal/profile file dropped"
    assert (dst / "memory" / "personal" / "notes" / "foo.md").exists(), \
        "nested personal/notes file dropped"
    assert (dst / "memory" / "personal" / "references" / "slack.md").exists(), \
        "nested personal/references file dropped"
    assert (dst / "memory" / "semantic" / "lessons" / "feedback_zzz.md").exists(), \
        "nested semantic/lessons feedback dropped"

    # Content preserved byte-for-byte
    assert (dst / "memory" / "personal" / "profile" / "boss.md").read_text() == \
        "# Boss profile\nNotes about boss.\n"
    assert (dst / "memory" / "personal" / "notes" / "foo.md").read_text() == \
        "# Foo note\nSome project context.\n"

    # Nested feedback under semantic/lessons/ also gets a lessons.jsonl row
    lessons_path = dst / "memory" / "semantic" / "lessons.jsonl"
    assert lessons_path.exists(), "nested feedback should still produce a lesson row"
    rows = [json.loads(line) for line in lessons_path.read_text().strip().splitlines()]
    assert len(rows) == 1
    assert "Always identify and run" in rows[0]["claim"]


def test_memory_md_hook_annotations_preserved(tmp_path):
    """Source MEMORY.md has `- [name](path) — hook` annotations that are
    human-curated signal. The regenerated index must preserve them so users
    don't lose the one-line descriptions of why each lesson matters."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"

    source_index = (
        "# Memory Index\n"
        "\n"
        "## Lessons (graduated)\n"
        "\n"
        "- [feedback_ci_parity](semantic/lessons/feedback_ci_parity.md) "
        "— always run the exact CI test command locally\n"
        "\n"
        "## Profile\n"
        "\n"
        "- [alice](personal/profile/alice.md)\n"
    )
    make_flat_memory(src, {
        "feedback_ci_parity.md": SAMPLE_FEEDBACK,
        "user_alice.md": SAMPLE_USER,
        "MEMORY.md": source_index,
    })

    result = run_migrate(src, dst)
    assert result.returncode == 0

    new_index = (dst / "memory" / "MEMORY.md").read_text()

    # The hook text after the em-dash is preserved on the matching entry
    assert "always run the exact CI test command locally" in new_index, \
        "hook annotation lost during migration"
    # Specifically the line for feedback_ci_parity carries the hook
    assert any(
        "feedback_ci_parity" in line and "always run the exact CI test command locally" in line
        for line in new_index.splitlines()
    ), "hook not associated with the correct entry"

    # Entries without a hook in the source don't get a phantom dash
    assert any(
        line.strip().endswith("personal/profile/alice.md)") and "—" not in line
        for line in new_index.splitlines()
    ), "entry without source hook should not have a trailing em-dash"


def test_frontmatter_fields_carried_into_lessons_jsonl(tmp_path):
    """Feedback frontmatter (name, type, originSessionId) must be preserved
    as structured fields on the lessons.jsonl row so future queries can
    filter on them. The companion .md preserves them already; this fills
    the gap on the structured side."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"

    feedback_with_frontmatter = """---
name: ci_parity
description: Always run the exact CI test command locally
type: feedback
originSessionId: dec1f620-3fcd-4399-8652-d1318b503cb9
---

Always run the exact CI test command locally.

**Why:** PR #39 passed local but failed CI.

**How to apply:** Read .circleci/config.yml first.
"""

    make_flat_memory(src, {"feedback_ci_parity.md": feedback_with_frontmatter})

    result = run_migrate(src, dst)
    assert result.returncode == 0

    lessons_path = dst / "memory" / "semantic" / "lessons.jsonl"
    row = json.loads(lessons_path.read_text().strip().splitlines()[0])

    assert row.get("name") == "ci_parity", \
        f"frontmatter `name` not in JSONL row: got {row.get('name')!r}"
    assert row.get("type") == "feedback", \
        f"frontmatter `type` not in JSONL row: got {row.get('type')!r}"
    # Field is `source_session_id`, NOT `origin_session_id` — the latter
    # would collide semantically with the v0.3 episode `origin` discriminator.
    assert row.get("source_session_id") == "dec1f620-3fcd-4399-8652-d1318b503cb9", \
        "frontmatter `originSessionId` not mapped to `source_session_id`"
    # v0.3 `origin` discriminator must NOT leak onto migrated lesson rows.
    assert "origin" not in row, "lesson rows must not carry the v0.3 episode `origin` field"
    assert "origin_session_id" not in row, "use `source_session_id`, not `origin_session_id`"


def test_install_migrate_symlinks_native_dir(tmp_path):
    """install.sh --migrate (default behavior) must, after copying files
    through, replace the source dir with a symlink pointing at
    $BRAIN_ROOT/memory so Claude Code's ongoing native writes flow into
    the brain. Original content goes to <source>.bak.<timestamp>."""
    src = tmp_path / "claude_native"
    brain = tmp_path / "brain"

    # Pre-build the brain root the way install.sh would have
    (brain / "memory").mkdir(parents=True)
    (brain / "tools").mkdir(parents=True)
    # Copy migrate.py + its _atomic dependency into the brain layout so the
    # install script's `python3 brain/tools/migrate.py ...` invocation can
    # resolve the path-relative `from _atomic import ...`.
    import shutil
    shutil.copy(MIGRATE_SCRIPT, brain / "tools" / "migrate.py")
    shutil.copy(REPO_ROOT / "agent" / "tools" / "migrate_dispatcher.py", brain / "tools")
    shutil.copy(REPO_ROOT / "agent" / "tools" / "cursor_adapter.py", brain / "tools")
    shutil.copy(REPO_ROOT / "agent" / "memory" / "_atomic.py", brain / "memory" / "_atomic.py")

    make_flat_memory(src, {"feedback_x.md": SAMPLE_FEEDBACK, "user_x.md": SAMPLE_USER})

    install_script = REPO_ROOT / "install.sh"
    env = os.environ.copy()
    env["BRAIN_ROOT"] = str(brain)
    # install.sh requires Python ≥ 3.10; the test runner may be on 3.9. Pin
    # to a known 3.10+ interpreter via the standard PYTHON_BIN escape hatch.
    if "PYTHON_BIN" not in env:
        for candidate in ("python3.13", "python3.12", "python3.11", "python3.10"):
            if shutil.which(candidate):
                env["PYTHON_BIN"] = candidate
                break

    result = subprocess.run(
        ["bash", str(install_script), "--migrate", str(src)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, f"install --migrate failed: {result.stderr}\n{result.stdout}"

    # Source path is now a symlink pointing at <brain>/memory.
    # Use os.path.realpath on both sides so we test canonical equivalence
    # (handles macOS /var → /private/var symlinks under tmp_path) AND
    # additionally assert the literal symlink target string matches what we
    # asked ln -s to write — this catches a class of "looks-the-same-after-
    # resolve but is actually a different intermediate target" bugs that the
    # production install.sh uses os.path.realpath to detect.
    assert src.is_symlink(), f"{src} should be a symlink after migration"
    assert os.path.realpath(src) == os.path.realpath(brain / "memory"), \
        f"symlink resolves wrong: {os.path.realpath(src)} != {os.path.realpath(brain / 'memory')}"
    # Literal target should be the absolute brain path (what install.sh wrote).
    assert os.readlink(src) == str(brain / "memory"), \
        f"literal symlink target wrong: {os.readlink(src)!r} != {str(brain / 'memory')!r}"

    # Backup of original content exists; the new naming includes a random
    # tag (ts.PID-RANDOM) so glob still matches.
    backups = list(tmp_path.glob("claude_native.bak.*"))
    assert len(backups) >= 1, "expected a timestamped backup directory"
    # Find one that's a real dir (the temp symlink, if it leaked, would also
    # match — but we delete those on rollback).
    backup_dirs = [b for b in backups if b.is_dir() and not b.is_symlink()]
    assert len(backup_dirs) >= 1, f"expected a real backup dir, got {backups}"
    assert (backup_dirs[0] / "feedback_x.md").exists(), \
        "backup must preserve original content"

    # Re-running install --migrate is idempotent: source is already the
    # right symlink, so this should not error or double-backup.
    result2 = subprocess.run(
        ["bash", str(install_script), "--migrate", str(src)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result2.returncode == 0, \
        f"re-running --migrate on already-symlinked source should be a no-op: {result2.stderr}"


def test_migrate_refuses_when_target_overlaps_source(tmp_path):
    """Self-recursion guard: refuse when src and dst are the same dir or
    when one contains the other. Prevents `migrate.py <symlinked-source>
    <brain>` from walking the brain itself and rewriting files mid-iteration."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "feedback_x.md").write_text(SAMPLE_FEEDBACK)

    # dst == src
    result = run_migrate(src, src)
    assert result.returncode != 0, "expected failure when dst == src"
    assert "overlap" in result.stderr.lower() or "refus" in result.stderr.lower(), \
        f"expected overlap/refuse message, got: {result.stderr}"

    # dst is parent of src
    result = run_migrate(src, tmp_path)
    assert result.returncode != 0, "expected failure when dst is parent of src"

    # src is parent of dst
    nested_dst = src / "nested-target"
    result = run_migrate(src, nested_dst)
    assert result.returncode != 0, "expected failure when src is parent of dst"


def test_migrate_skips_symlinked_files_in_source(tmp_path):
    """Security: a symlinked .md in source must NOT be read through.
    A malicious or accidental `feedback_pwned.md -> /etc/hosts` should
    not pull arbitrary content into the brain."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    # Real feedback file alongside the symlinked one
    (src / "feedback_real.md").write_text(SAMPLE_FEEDBACK)
    # Drop a target file with sensitive-looking content elsewhere
    sensitive = tmp_path / "sensitive.txt"
    sensitive.write_text("SECRET_TOKEN=abc123\n")
    # Symlink it into the source dir under a feedback_*.md filename
    (src / "feedback_pwned.md").symlink_to(sensitive)

    result = run_migrate(src, dst)
    assert result.returncode == 0

    # The real file IS migrated
    assert (dst / "memory" / "semantic" / "lessons" / "feedback_real.md").exists()
    # The symlinked file is NOT — secrets must not be exfiltrated through symlinks
    assert not (dst / "memory" / "semantic" / "lessons" / "feedback_pwned.md").exists(), \
        "symlinked .md must not be migrated (would exfiltrate symlink target content)"
    # Defensive: scan the whole brain output for the secret content
    for path in (dst / "memory").rglob("*.md"):
        assert "SECRET_TOKEN=abc123" not in path.read_text(), \
            f"symlinked content leaked into {path}"


def test_migrate_preserves_nested_feedback_subdirectory(tmp_path):
    """`<src>/semantic/lessons/sub/feedback_x.md` must NOT lose the `sub/`
    segment during migration. The companion lands at the matching nested
    location under `<dst>/memory/semantic/lessons/sub/`, and the lessons.jsonl
    row's `original_markdown_path` records that nested location."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    nested_dir = src / "semantic" / "lessons" / "sub-archive"
    nested_dir.mkdir(parents=True)
    (nested_dir / "feedback_archived.md").write_text(SAMPLE_FEEDBACK)

    result = run_migrate(src, dst)
    assert result.returncode == 0

    expected_companion = dst / "memory" / "semantic" / "lessons" / "sub-archive" / "feedback_archived.md"
    assert expected_companion.exists(), \
        f"nested feedback companion lost; expected at {expected_companion}"

    rows = (dst / "memory" / "semantic" / "lessons.jsonl").read_text().strip().splitlines()
    assert len(rows) == 1
    row = json.loads(rows[0])
    # `original_markdown_path` records the nested position so future readers
    # can find the long-form text without scanning.
    assert "sub-archive" in row["original_markdown_path"], \
        f"original_markdown_path lost nested segment: {row['original_markdown_path']}"


def test_install_migrate_refuses_pre_existing_unrelated_symlink(tmp_path):
    """If the source is already a symlink to somewhere OTHER than the brain,
    install.sh must refuse rather than silently overwrite the user's topology."""
    src = tmp_path / "claude_native"
    brain = tmp_path / "brain"
    elsewhere = tmp_path / "user_other_dir"
    elsewhere.mkdir()
    (elsewhere / "marker.md").write_text("# user data unrelated to brain\n")

    (brain / "memory").mkdir(parents=True)
    (brain / "tools").mkdir(parents=True)
    import shutil
    shutil.copy(MIGRATE_SCRIPT, brain / "tools" / "migrate.py")
    shutil.copy(REPO_ROOT / "agent" / "tools" / "migrate_dispatcher.py", brain / "tools")
    shutil.copy(REPO_ROOT / "agent" / "tools" / "cursor_adapter.py", brain / "tools")

    # User pre-symlinks src to their own dir (not the brain)
    src.symlink_to(elsewhere)

    install_script = REPO_ROOT / "install.sh"
    env = os.environ.copy()
    env["BRAIN_ROOT"] = str(brain)
    if "PYTHON_BIN" not in env:
        for candidate in ("python3.13", "python3.12", "python3.11", "python3.10"):
            if shutil.which(candidate):
                env["PYTHON_BIN"] = candidate
                break

    result = subprocess.run(
        ["bash", str(install_script), "--migrate", str(src)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode != 0, \
        "install.sh must refuse to overwrite a pre-existing user-owned symlink"
    assert "symlink" in result.stderr.lower() and "refus" in result.stderr.lower(), \
        f"expected refusal message about symlink, got: {result.stderr}"

    # User's elsewhere dir must be intact
    assert (elsewhere / "marker.md").exists(), \
        "user's pre-existing symlink target must not be touched"
    # And the symlink itself unchanged
    assert src.is_symlink()
    assert os.readlink(src) == str(elsewhere)


def test_install_migrate_rejects_conflicting_symlink_flags(tmp_path):
    """Passing both --symlink-native and --no-symlink is almost always a
    wrapper-script bug. install.sh must refuse rather than silently use
    whichever appeared last."""
    src = tmp_path / "src"
    brain = tmp_path / "brain"
    src.mkdir()
    (src / "feedback_x.md").write_text(SAMPLE_FEEDBACK)
    (brain / "memory").mkdir(parents=True)
    (brain / "tools").mkdir(parents=True)
    import shutil
    shutil.copy(MIGRATE_SCRIPT, brain / "tools" / "migrate.py")
    shutil.copy(REPO_ROOT / "agent" / "tools" / "migrate_dispatcher.py", brain / "tools")
    shutil.copy(REPO_ROOT / "agent" / "tools" / "cursor_adapter.py", brain / "tools")

    install_script = REPO_ROOT / "install.sh"
    env = os.environ.copy()
    env["BRAIN_ROOT"] = str(brain)
    if "PYTHON_BIN" not in env:
        for candidate in ("python3.13", "python3.12", "python3.11", "python3.10"):
            if shutil.which(candidate):
                env["PYTHON_BIN"] = candidate
                break

    result = subprocess.run(
        ["bash", str(install_script),
         "--migrate", str(src), "--symlink-native", "--no-symlink"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 2, "expected exit 2 for mutually exclusive flags"
    assert "mutually exclusive" in result.stderr.lower(), \
        f"expected mutually-exclusive error, got: {result.stderr}"


def test_hook_lookup_survives_flat_prefix_strip(tmp_path):
    """Source MEMORY.md keys hooks under the source stem (`user_alice`),
    but the migrated file lives at `alice.md` (prefix stripped). The
    regenerated index must still surface the hook on the migrated entry."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    make_flat_memory(src, {
        "user_alice.md": SAMPLE_USER,
        "reference_api.md": SAMPLE_REFERENCE,
        "MEMORY.md": (
            "# Memory Index\n\n"
            "- [user_alice](personal/profile/alice.md) — alice is the EM\n"
            "- [reference_api](personal/references/api.md) — internal API docs\n"
        ),
    })

    result = run_migrate(src, dst)
    assert result.returncode == 0

    new_index = (dst / "memory" / "MEMORY.md").read_text()
    # The migrated entry shows up under `alice` (stripped) but the hook
    # text from the source `user_alice` entry must follow it.
    assert "alice is the EM" in new_index, \
        f"hook lost when source stem was prefix-stripped:\n{new_index}"
    assert "internal API docs" in new_index, \
        f"reference_ hook lost during prefix strip:\n{new_index}"


def test_recursive_walk_distinct_lesson_ids_for_same_basename(tmp_path):
    """Two feedback files sharing a basename in different nested subdirs
    must produce DISTINCT lesson IDs so neither gets dropped by the
    `by_id` de-dup in write_lessons_jsonl."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    a_dir = src / "semantic" / "lessons" / "a"
    b_dir = src / "semantic" / "lessons" / "b"
    a_dir.mkdir(parents=True)
    b_dir.mkdir(parents=True)
    (a_dir / "feedback_rule.md").write_text(SAMPLE_FEEDBACK)
    (b_dir / "feedback_rule.md").write_text(
        SAMPLE_FEEDBACK.replace("CI parity", "Different rule")
    )

    result = run_migrate(src, dst)
    assert result.returncode == 0

    rows = (dst / "memory" / "semantic" / "lessons.jsonl").read_text().strip().splitlines()
    assert len(rows) == 2, \
        f"expected 2 lessons (one per nested subdir); got {len(rows)} — IDs collided"
    ids = {json.loads(line)["id"] for line in rows}
    assert len(ids) == 2, f"expected 2 distinct ids; got {ids}"


def test_target_shaped_filenames_not_prefix_stripped(tmp_path):
    """If source already has `personal/profile/user_alice.md` (target-shaped
    AND prefix-named), the file must round-trip verbatim — NOT get demangled
    to `personal/profile/alice.md`. That would break round-trip and could
    silently overwrite a sibling `alice.md`."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    (src / "personal" / "profile").mkdir(parents=True)
    (src / "personal" / "profile" / "user_alice.md").write_text("# Alice (verbose name)\n")
    # A sibling that COULD have been collided with
    (src / "personal" / "profile" / "alice.md").write_text("# Alice (canonical)\n")

    result = run_migrate(src, dst)
    assert result.returncode == 0

    # Both files survive, neither collides
    assert (dst / "memory" / "personal" / "profile" / "user_alice.md").exists(), \
        "target-shaped file was demangled — prefix stripping should not apply here"
    assert (dst / "memory" / "personal" / "profile" / "alice.md").exists()
    # Content of each is its own
    assert "verbose name" in (dst / "memory" / "personal" / "profile" / "user_alice.md").read_text()
    assert "canonical" in (dst / "memory" / "personal" / "profile" / "alice.md").read_text()


def test_install_migrate_handles_trailing_slash_in_source(tmp_path):
    """Shell completion appends `/` to dir args. The symlink swap must still
    end up with a working symlink at the de-slashed path, not a half-failed
    state where the source is gone but the symlink wasn't installed."""
    src = tmp_path / "claude_native"
    src.mkdir()
    (src / "feedback_x.md").write_text(SAMPLE_FEEDBACK)
    brain = tmp_path / "brain"
    (brain / "memory").mkdir(parents=True)
    (brain / "tools").mkdir(parents=True)
    import shutil
    shutil.copy(MIGRATE_SCRIPT, brain / "tools" / "migrate.py")
    shutil.copy(REPO_ROOT / "agent" / "tools" / "migrate_dispatcher.py", brain / "tools")
    shutil.copy(REPO_ROOT / "agent" / "tools" / "cursor_adapter.py", brain / "tools")
    shutil.copy(REPO_ROOT / "agent" / "memory" / "_atomic.py", brain / "memory" / "_atomic.py")

    install_script = REPO_ROOT / "install.sh"
    env = os.environ.copy()
    env["BRAIN_ROOT"] = str(brain)
    if "PYTHON_BIN" not in env:
        for candidate in ("python3.13", "python3.12", "python3.11", "python3.10"):
            if shutil.which(candidate):
                env["PYTHON_BIN"] = candidate
                break

    # Pass the source path WITH a trailing slash — the kind of input
    # shell completion produces.
    src_with_slash = str(src) + "/"
    result = subprocess.run(
        ["bash", str(install_script), "--migrate", src_with_slash],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, \
        f"install --migrate failed on trailing-slash source:\n{result.stderr}\n{result.stdout}"
    # Source path (without slash) is now a symlink
    assert src.is_symlink(), \
        f"{src} should be a symlink after migration; ls -la: {list(tmp_path.iterdir())}"
    assert os.readlink(src) == str(brain / "memory")


def test_install_migrate_uses_absolute_brain_path_in_symlink(tmp_path):
    """If BRAIN_ROOT is given as a relative path, the symlink target must
    still be absolute — otherwise the symlink resolves relative to its own
    parent dir (i.e., the source's parent), which is usually broken."""
    src = tmp_path / "claude_native"
    src.mkdir()
    (src / "feedback_x.md").write_text(SAMPLE_FEEDBACK)
    brain = tmp_path / "brain"
    (brain / "memory").mkdir(parents=True)
    (brain / "tools").mkdir(parents=True)
    import shutil
    shutil.copy(MIGRATE_SCRIPT, brain / "tools" / "migrate.py")
    shutil.copy(REPO_ROOT / "agent" / "tools" / "migrate_dispatcher.py", brain / "tools")
    shutil.copy(REPO_ROOT / "agent" / "tools" / "cursor_adapter.py", brain / "tools")
    shutil.copy(REPO_ROOT / "agent" / "memory" / "_atomic.py", brain / "memory" / "_atomic.py")

    install_script = REPO_ROOT / "install.sh"
    env = os.environ.copy()
    if "PYTHON_BIN" not in env:
        for candidate in ("python3.13", "python3.12", "python3.11", "python3.10"):
            if shutil.which(candidate):
                env["PYTHON_BIN"] = candidate
                break

    # Run install.sh from `tmp_path` and pass BRAIN_ROOT as a RELATIVE path.
    rel_brain = "brain"  # relative to cwd=tmp_path
    rel_src = "claude_native"
    env["BRAIN_ROOT"] = rel_brain
    result = subprocess.run(
        ["bash", str(install_script), "--migrate", rel_src],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, \
        f"install --migrate failed with relative BRAIN_ROOT:\n{result.stderr}\n{result.stdout}"
    assert src.is_symlink()
    # The literal symlink target must be absolute. A relative target like
    # `brain/memory` would resolve relative to `<src>'s parent / brain/memory`
    # — usually broken.
    target = os.readlink(src)
    assert os.path.isabs(target), \
        f"symlink target must be absolute; got {target!r}"
    # It resolves to the real brain memory dir.
    assert os.path.realpath(src) == os.path.realpath(brain / "memory")


def test_lossless_roundtrip_realistic_claude_dir(tmp_path):
    """End-to-end proof: every byte of every input .md file lands somewhere
    addressable in the new brain. Mix of flat + nested + frontmatter +
    MEMORY.md hooks. Idempotent under re-run."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"

    # Flat at root
    make_flat_memory(src, {
        "feedback_flat.md": SAMPLE_FEEDBACK,
        "user_alice.md": SAMPLE_USER,
        "project_foo.md": SAMPLE_PROJECT,
        "reference_r.md": SAMPLE_REFERENCE,
    })

    # Nested target-shaped paths
    (src / "personal" / "profile").mkdir(parents=True)
    (src / "personal" / "profile" / "boss.md").write_text("Profile content\n")
    (src / "personal" / "notes").mkdir(parents=True)
    (src / "personal" / "notes" / "session-2026-04-29.md").write_text("Session notes\n")
    (src / "personal" / "references").mkdir(parents=True)
    (src / "personal" / "references" / "vulcan.md").write_text("Vulcan ref\n")
    (src / "semantic" / "lessons").mkdir(parents=True)
    (src / "semantic" / "lessons" / "feedback_nested.md").write_text(
        SAMPLE_FEEDBACK.replace("CI parity", "Nested rule")
    )

    # MEMORY.md with hooks
    (src / "MEMORY.md").write_text(
        "# Memory Index\n\n"
        "## Lessons (graduated)\n\n"
        "- [feedback_flat](semantic/lessons/feedback_flat.md) — flat root hook\n"
        "- [feedback_nested](semantic/lessons/feedback_nested.md) — nested hook\n"
    )

    # Snapshot every input file's content
    inputs = {}
    for path in src.rglob("*.md"):
        if path.name == "MEMORY.md":
            continue  # MEMORY.md is intentionally regenerated
        inputs[path.name] = path.read_text()

    result = run_migrate(src, dst)
    assert result.returncode == 0

    # For every input file, prove its bytes are addressable from the new brain.
    # Acceptable target locations:
    #   1. semantic/lessons/<name>.md (companion for feedback_*)
    #   2. personal/<sub>/<stripped-name>.md (typed personal files)
    #   3. lessons.jsonl row + companion (for feedback files)
    # Track which target paths have been claimed so identical-content inputs
    # (e.g., two empty notes) don't both match the same target.
    claimed: set[Path] = set()
    found: dict[str, Path] = {}
    for name, content in inputs.items():
        for candidate in (dst / "memory").rglob("*.md"):
            if candidate in claimed:
                continue
            if candidate.read_text() == content:
                found[name] = candidate
                claimed.add(candidate)
                break
        assert name in found, \
            f"input file {name} content not addressable anywhere under {dst / 'memory'}"

    # Hooks preserved in regenerated index
    new_index = (dst / "memory" / "MEMORY.md").read_text()
    assert "flat root hook" in new_index, "flat hook lost"
    assert "nested hook" in new_index, "nested hook lost"

    # Idempotent: re-running produces identical jsonl + identical index
    first_jsonl = (dst / "memory" / "semantic" / "lessons.jsonl").read_text()
    first_index = new_index
    result2 = run_migrate(src, dst)
    assert result2.returncode == 0
    second_jsonl = (dst / "memory" / "semantic" / "lessons.jsonl").read_text()
    second_index = (dst / "memory" / "MEMORY.md").read_text()
    assert first_jsonl == second_jsonl, "lessons.jsonl not idempotent"
    assert first_index == second_index, "MEMORY.md not idempotent"
