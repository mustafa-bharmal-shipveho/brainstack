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
    # reviewed=True: this test pins the DURABLE lesson shape. The staged
    # default is covered by TestReviewGate below.
    path = write_lesson(
        "always use /agent-team for development", brain_root=brain,
        reviewed=True,
    )
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
    # reviewed=True keeps this focused on the overwrite contract for
    # durable lessons (the original intent), not the staging default.
    write_lesson("first", name="dup", brain_root=brain, reviewed=True)
    path = write_lesson(
        "second", name="dup", brain_root=brain, overwrite=True, reviewed=True,
    )
    assert "second" in path.read_text()


def test_write_lesson_missing_brain_root_errors(tmp_path: Path) -> None:
    """If ~/.agent/memory/semantic/lessons/ doesn't exist, surface a useful error."""
    with pytest.raises(FileNotFoundError, match="lessons dir not found"):
        write_lesson("hi", brain_root=tmp_path / "no-brain")


def test_write_lesson_includes_iso_timestamp(brain: Path) -> None:
    # reviewed=True: pins the durable shape's timestamp; staging metadata
    # is asserted separately in TestReviewGate.
    path = write_lesson("hi", brain_root=brain, reviewed=True)
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
    # reviewed=True: the round trip under test is durable-remember then
    # forget; the staged path has its own lifecycle (pending --review).
    write_lesson("always use /agent-team", brain_root=brain, reviewed=True)
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


# ---------- review gate (trust/security workstream) ----------


def _frontmatter_of(path: Path) -> dict:
    """Parse the YAML frontmatter block of a lesson file."""
    import yaml

    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"no frontmatter block in {path}"
    fm_block = text.split("---\n", 2)[1]
    fm = yaml.safe_load(fm_block)
    assert isinstance(fm, dict)
    return fm


class TestReviewGate:
    """`recall remember` is a write path any agent (or injected prompt)
    can trigger. Default writes must be STAGED for human review, not
    silently durable: needs_review + review_reason in frontmatter so the
    retrieval review policy demotes them until a human accepts. Only the
    explicit --reviewed flag (a human at the CLI) writes a durable lesson.
    """

    def test_default_write_is_staged(self, brain: Path) -> None:
        path = write_lesson(
            "prefer placeholder identifiers like Acme in fixtures",
            brain_root=brain,
        )
        fm = _frontmatter_of(path)
        assert fm.get("needs_review") is True
        assert fm.get("review_reason") == "unreviewed-remember"
        assert fm.get("created_by") == "recall-remember"

    def test_reviewed_write_is_durable(self, brain: Path) -> None:
        path = write_lesson(
            "prefer placeholder identifiers like Acme in fixtures",
            brain_root=brain,
            reviewed=True,
        )
        fm = _frontmatter_of(path)
        assert fm.get("reviewed_by") == "human-cli"
        assert "needs_review" not in fm, (
            "a human-reviewed lesson must not carry the staging flag"
        )

    def test_cli_default_output_mentions_staging(self, brain: Path) -> None:
        from typer.testing import CliRunner

        from recall.cli import app

        runner = CliRunner()
        result = runner.invoke(
            app,
            ["remember", "Alice says keep diffs small",
             "--brain-root", str(brain)],
        )
        assert result.exit_code == 0, result.output
        assert "staged" in result.output.lower()
        assert "pending --review" in result.output

    def test_cli_reviewed_flag(self, brain: Path) -> None:
        from typer.testing import CliRunner

        from recall.cli import app

        # CliRunner drives the command with a non-TTY stdin (exactly how an
        # agent would). A durable --reviewed write off a TTY needs the
        # explicit human-decision ack; this test asserts the durable path
        # once acknowledged.
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["remember", "Alice says keep diffs small", "--reviewed",
             "--non-interactive-ack", "--brain-root", str(brain)],
        )
        assert result.exit_code == 0, result.output
        assert "staged" not in result.output.lower()

    def test_cli_reviewed_without_ack_off_tty_is_refused(self, brain: Path) -> None:
        """A durable --reviewed write off a TTY (no ack) must be refused so an
        agent or injected prompt cannot silently bypass review staging. Nothing
        is written to disk."""
        from typer.testing import CliRunner

        from recall.cli import app

        runner = CliRunner()
        result = runner.invoke(
            app,
            ["remember", "injected durable lesson", "--reviewed",
             "--brain-root", str(brain)],
        )
        assert result.exit_code == 4, result.output
        assert "not a TTY" in result.output
        # No lesson file was created.
        assert list((brain / LESSONS_SUBDIR).glob("*.md")) == []

    def test_cli_default_staged_off_tty_is_allowed(self, brain: Path) -> None:
        """The default (staged) path is always allowed off a TTY; staging is
        the safe outcome, and only the durable --reviewed bypass is gated."""
        from typer.testing import CliRunner

        from recall.cli import app

        runner = CliRunner()
        result = runner.invoke(
            app,
            ["remember", "agent-staged advice", "--brain-root", str(brain)],
        )
        assert result.exit_code == 0, result.output
        fm = _frontmatter_of(next((brain / LESSONS_SUBDIR).glob("*.md")))
        assert fm.get("needs_review") is True

    def test_pending_review_without_tty_does_not_modify(
        self, brain: Path, monkeypatch
    ) -> None:
        """`recall pending --review` is TTY-gated. Driven without a TTY
        (exactly how an agent would call it), it must not modify any
        staged lesson: either exit non-zero or skip cleanly with an
        error message. Lessons on disk stay byte-identical."""
        from typer.testing import CliRunner

        from recall.cli import app

        staged = brain / LESSONS_SUBDIR / "staged-one.md"
        staged.write_text(
            "---\n"
            "name: staged-one\n"
            "description: synthetic staged lesson from Acme triage\n"
            "type: lesson\n"
            "source: recall-remember\n"
            "created_by: recall-remember\n"
            "created: 2026-06-01T00:00:00+00:00\n"
            "needs_review: true\n"
            "review_reason: unreviewed-remember\n"
            "---\n"
            "\n"
            "Alice prefers small reviewable diffs.\n",
            encoding="utf-8",
        )
        before = staged.read_bytes()

        # Belt and braces: the legacy --review path hands off via
        # os.execv, which would replace the test process. Record instead.
        import os as _os
        execv_calls: list = []
        monkeypatch.setattr(
            _os, "execv", lambda *a, **kw: execv_calls.append(a)
        )
        # CliRunner.invoke swaps sys.stdin for a non-TTY stream, so the
        # command runs exactly as an agent (no TTY) would drive it.

        runner = CliRunner()
        result = runner.invoke(
            app, ["pending", "--review", "--brain", str(brain)],
        )

        assert staged.read_bytes() == before, (
            "pending --review modified a staged lesson without a TTY"
        )
        assert execv_calls == [], (
            "pending --review must not exec the interactive triage REPL "
            "without a TTY"
        )
        # Non-zero exit or a clean skip with an explanatory message.
        assert result.exit_code != 0 or result.output.strip(), (
            "expected a non-zero exit or an explanatory message"
        )
