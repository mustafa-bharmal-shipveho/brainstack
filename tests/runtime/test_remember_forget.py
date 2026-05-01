"""v0.4.0: persistent remember + forget."""
from __future__ import annotations

from pathlib import Path

import pytest

from recall.forget import ARCHIVED_SUBDIR, archive_lesson
from recall.remember import LESSONS_SUBDIR, _slugify, write_lesson


@pytest.fixture
def brain(tmp_path: Path) -> Path:
    """A skeleton ~/.agent/-shaped tree for tests."""
    (tmp_path / LESSONS_SUBDIR).mkdir(parents=True)
    return tmp_path


# ---------- slugify ----------

def test_slugify_handles_natural_phrasing() -> None:
    assert _slugify("always use /agent-team for development") == "always-use-agent-team-for-development"


def test_slugify_truncates_long_text() -> None:
    s = _slugify("a" * 200)
    assert len(s) <= 60


def test_slugify_falls_back_to_lesson_for_empty() -> None:
    assert _slugify("") == "lesson"
    assert _slugify("!!!") == "lesson"


def test_slugify_strips_non_alphanumeric() -> None:
    assert _slugify("hello, world! @#$%") == "hello-world"


# ---------- write_lesson ----------

def test_write_lesson_creates_file_with_frontmatter(brain: Path) -> None:
    path = write_lesson("always use /agent-team for development", brain_root=brain)
    assert path.exists()
    text = path.read_text()
    assert "---\n" in text
    assert "type: lesson" in text
    assert "source: recall-remember" in text
    assert "always use /agent-team for development" in text


def test_write_lesson_uses_explicit_name(brain: Path) -> None:
    path = write_lesson("body", name="my-custom-name", brain_root=brain)
    assert path.name == "my-custom-name.md"


def test_write_lesson_default_slug_from_first_line(brain: Path) -> None:
    path = write_lesson("first line\nsecond line\nthird", brain_root=brain)
    assert "first-line" in path.name


def test_write_lesson_rejects_empty_text(brain: Path) -> None:
    with pytest.raises(ValueError):
        write_lesson("   \n\n  ", brain_root=brain)


def test_write_lesson_refuses_overwrite_by_default(brain: Path) -> None:
    write_lesson("hello", name="dup", brain_root=brain)
    with pytest.raises(FileExistsError):
        write_lesson("again", name="dup", brain_root=brain)


def test_write_lesson_overwrite_flag_replaces(brain: Path) -> None:
    write_lesson("first", name="dup", brain_root=brain)
    path = write_lesson("second", name="dup", brain_root=brain, overwrite=True)
    assert "second" in path.read_text()


def test_write_lesson_missing_brain_root_errors(tmp_path: Path) -> None:
    """If ~/.agent/memory/semantic/lessons/ doesn't exist, surface a useful error."""
    with pytest.raises(FileNotFoundError, match="lessons dir not found"):
        write_lesson("hi", brain_root=tmp_path / "no-brain")


def test_write_lesson_includes_iso_timestamp(brain: Path) -> None:
    path = write_lesson("hi", brain_root=brain)
    text = path.read_text()
    # ISO 8601 with timezone — example: 2026-05-01T07:42:31.123456+00:00
    assert "created: " in text
    # Just sanity-check the prefix
    import re
    assert re.search(r"created: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}", text)


# ---------- archive_lesson ----------

def test_archive_lesson_moves_to_archived(brain: Path) -> None:
    write_lesson("keep this idea", name="my-lesson", brain_root=brain)
    result = archive_lesson("my-lesson", brain_root=brain)
    assert result.archived_path is not None
    assert result.archived_path.exists()
    assert (brain / ARCHIVED_SUBDIR) in result.archived_path.parents
    # original is gone
    assert not (brain / LESSONS_SUBDIR / "my-lesson.md").exists()


def test_archive_lesson_substring_match(brain: Path) -> None:
    write_lesson("a", name="postgres-locking", brain_root=brain)
    write_lesson("b", name="other-thing", brain_root=brain)
    result = archive_lesson("locking", brain_root=brain)
    assert result.archived_path is not None
    assert "postgres-locking" in result.archived_path.name


def test_archive_lesson_ambiguous_returns_candidates(brain: Path) -> None:
    write_lesson("a", name="postgres-locking", brain_root=brain)
    write_lesson("b", name="postgres-deadlock", brain_root=brain)
    result = archive_lesson("postgres", brain_root=brain)
    assert result.archived_path is None
    assert len(result.candidates) == 2


def test_archive_lesson_no_match(brain: Path) -> None:
    write_lesson("a", name="some-lesson", brain_root=brain)
    result = archive_lesson("does-not-exist", brain_root=brain)
    assert result.archived_path is None
    assert result.candidates == []


def test_archive_lesson_missing_brain_root(tmp_path: Path) -> None:
    """Returns empty result, doesn't crash."""
    result = archive_lesson("anything", brain_root=tmp_path / "no-brain")
    assert result.archived_path is None
    assert result.candidates == []


def test_archive_lesson_creates_archive_dir_if_missing(brain: Path) -> None:
    write_lesson("hi", name="single", brain_root=brain)
    archive_dir = brain / ARCHIVED_SUBDIR
    assert not archive_dir.exists()
    archive_lesson("single", brain_root=brain)
    assert archive_dir.exists()


# ---------- end-to-end: remember then forget ----------

def test_round_trip_remember_then_forget(brain: Path) -> None:
    write_lesson("always use /agent-team", brain_root=brain)
    # Initial state: 1 lesson in the lessons dir
    files = list((brain / LESSONS_SUBDIR).glob("*.md"))
    assert len(files) == 1

    archive_lesson("agent-team", brain_root=brain)
    # After: 0 lessons, 1 archived
    files = list((brain / LESSONS_SUBDIR).glob("*.md"))
    assert len(files) == 0
    archived = list((brain / ARCHIVED_SUBDIR).glob("*.md"))
    assert len(archived) == 1
    assert "always-use-agent-team" in archived[0].name
