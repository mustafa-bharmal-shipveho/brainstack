"""Tests for `recall lint --fix-digests` (recall/lint_digests.py).

Why this surface exists
-----------------------
Every session digest under `memory/semantic/digests/` HAS a frontmatter
block (session_id, source, started_at, cwd, domain_tags, outcome,
salience) and NONE of them carry `name` / `description` / `type`. Recall
weights `name` + `description` when it builds the indexed text and uses
`type` for filtering, so a digest without them is retrievable only by its
raw body. `--fix-digests` backfills exactly those three keys.

The hard constraints this file pins:

  1. It SPLICES into the existing frontmatter block. It never fabricates a
     block, never rewrites the body, and never touches the file's newline
     style. A digest is the user's only copy of that session.
  2. It only ever proposes the MISSING keys, so a second run is a no-op.
  3. It is dry-run by default. Writing requires `--apply`.
  4. A digest whose frontmatter YAML doesn't parse is SKIPPED with a
     reason, not guessed at — `recall lint --repair` owns that case.

`recall.lint_digests` does not exist yet; the `ld` fixture imports it
lazily so this module still COLLECTS in the red phase (a missing
implementation shows up as failing tests, not a collection error).
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from recall import lint
from recall.cli import app
from recall.frontmatter import parse_path


@pytest.fixture
def ld():
    """The module under test. Imported lazily — see module docstring."""
    import recall.lint_digests

    return recall.lint_digests


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# Fixtures — real-shaped digests
# ---------------------------------------------------------------------------

# The frontmatter block `agent/tools/_digest_render.render_markdown` emits
# today, minus the three keys the backfill adds. Keys are listed in the
# renderer's order so "splice at the top, keep the rest" is checkable.
_FM_LINES = [
    "session_id: ff1a2018-0d7c-4b31-9c0e-3a1e2b4c5d6e",
    "source: claude",
    "created_by: digest-adapter",
    "started_at: 2026-03-24T09:12:03Z",
    "ended_at: 2026-03-24T11:48:22Z",
    "cwd: /work/facilities",
    "git_branch: main",
    "project_slug: facilities",
    "model: claude-haiku-4-5",
    "domain_tags: [facilities, roadmap]",
    "outcome: completed",
    "salience: 8",
]

_BODY = """
# Q2 2026 Facilities Roadmap Setup: Linear Projects, Gantt Visualization, and Capacity Planning

## What you did

Set up the complete Q2 2026 Facilities roadmap: created 13 projects in Linear with sequencing rules, then generated a Gantt view. Second sentence that must not appear in the description.

## What was learned

Sequencing rules have to be declared before the Gantt view is generated.

## Decisions

- Keep the roadmap in Linear
- Regenerate the Gantt weekly

## Files touched

- `tools/roadmap.py`
"""


def _digest_text(fm_lines: list[str], body: str, newline: str = "\n") -> str:
    text = "---\n" + "\n".join(fm_lines) + "\n---\n" + body
    if newline != "\n":
        text = text.replace("\n", newline)
    return text


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


@pytest.fixture
def digest_brain(tmp_path: Path) -> Path:
    """A memory root with four digests covering every branch.

    Returns the MEMORY root (what `recall lint --brain` is given by
    default: `resolve_brain_home()` == `~/.agent/memory`).
    """
    memory = tmp_path / ".agent" / "memory"
    digests = memory / "semantic" / "digests"

    # 1. The common case: full frontmatter, none of the three keys.
    _write(digests / "2026-03-24__q2-2026-facilities-roadmap__ff1a201a.md",
           _digest_text(_FM_LINES, _BODY))

    # 2. Same, but CRLF throughout — newline style must survive the splice.
    _write(digests / "2026-03-25__crlf-session__aa11bb22.md",
           _digest_text(_FM_LINES, _BODY, newline="\r\n"))

    # 3. Already complete — must be left alone entirely.
    _write(digests / "2026-03-26__already-complete__cc33dd44.md",
           _digest_text(
               ["name: already-complete-digest",
                "description: A digest that already carries all three keys.",
                "type: digest", *_FM_LINES],
               _BODY))

    # 4. Unparseable frontmatter (unquoted value with a colon) — the
    #    backfill must refuse to touch it and say why.
    _write(digests / "2026-03-27__unparseable__ee55ff66.md",
           _digest_text(
               [*_FM_LINES[:-2],
                "outcome: Scope negotiated: 502 backend error deferred to triage",
                "salience: 6"],
               _BODY))

    return memory


@pytest.fixture
def clean_digest_brain(tmp_path: Path) -> Path:
    """Same shape as `digest_brain` but with NO unparseable digest, so a
    full `--fix-digests --apply` run leaves zero residual lint findings
    (needed to pin the CLI's 1-then-0 exit-code contract)."""
    memory = tmp_path / "clean" / "memory"
    digests = memory / "semantic" / "digests"
    _write(digests / "2026-04-01__first-session__11112222.md",
           _digest_text(_FM_LINES, _BODY))
    _write(digests / "2026-04-02__second-session__33334444.md",
           _digest_text(_FM_LINES, _BODY))
    return memory


def _by_name(fixes) -> dict:
    return {f.file.name: f for f in fixes}


def _fm_tail(path: Path) -> str:
    """Everything from the newline before the closing `---` to EOF.

    Read through the same helper the writer uses, so "body bytes
    untouched" is checked against the exact region the splice must not
    move.
    """
    bounds = lint._read_frontmatter_bounds(path)
    assert bounds is not None, f"{path} lost its frontmatter block"
    raw, _newline, _body_start, fm_end = bounds
    return raw[fm_end:]


def _fm_keys_in_order(path: Path) -> list[str]:
    bounds = lint._read_frontmatter_bounds(path)
    assert bounds is not None
    raw, newline, body_start, fm_end = bounds
    keys = []
    for line in raw[body_start:fm_end].split(newline):
        m = re.match(r"^([A-Za-z_][\w\-]*)\s*:", line)
        if m:
            keys.append(m.group(1))
    return keys


# ---------------------------------------------------------------------------
# Root resolution
# ---------------------------------------------------------------------------

class TestDigestsDir:
    def test_accepts_memory_root_and_brain_root(self, ld, digest_brain):
        """`--brain` may be `~/.agent/memory` (the lint default) or the
        brain root itself. Both must find the same digests dir."""
        expected = digest_brain / "semantic" / "digests"
        assert ld.digests_dir(digest_brain) == expected
        assert ld.digests_dir(digest_brain.parent) == expected

    def test_returns_none_when_absent(self, ld, tmp_path):
        assert ld.digests_dir(tmp_path / "nothing-here") is None

    def test_plan_on_root_without_digests_is_empty(self, ld, tmp_path):
        assert ld.plan_digest_fixes(tmp_path) == []


# ---------------------------------------------------------------------------
# Planning rules
# ---------------------------------------------------------------------------

class TestPlanRules:
    def test_plan_lists_only_missing_keys(self, ld, digest_brain):
        fixes = _by_name(ld.plan_digest_fixes(digest_brain))
        fix = fixes["2026-03-24__q2-2026-facilities-roadmap__ff1a201a.md"]
        assert set(fix.missing) == set(ld.DIGEST_REQUIRED)
        assert set(fix.proposed) == {"name", "description", "type"}
        assert fix.skipped_reason == ""
        assert fix.proposed["type"] == ld.DIGEST_TYPE == "digest"

    def test_existing_values_are_never_overwritten(self, ld, tmp_path):
        """A digest that has `type` but not `name`/`description` gets only
        the two it lacks — proposing all three would clobber curated
        values on the next backfill."""
        memory = tmp_path / "memory"
        f = _write(memory / "semantic" / "digests" / "partial__aa.md",
                   _digest_text(["type: digest", *_FM_LINES], _BODY))
        fix = _by_name(ld.plan_digest_fixes(memory))[f.name]
        assert set(fix.missing) == {"name", "description"}
        assert "type" not in fix.proposed

    def test_blank_value_counts_as_missing(self, ld, tmp_path):
        """`description:` with an empty value is not a description."""
        memory = tmp_path / "memory"
        f = _write(memory / "semantic" / "digests" / "blank__bb.md",
                   _digest_text(["name: blank-bb", "description: ", "type: digest",
                                 *_FM_LINES], _BODY))
        fix = _by_name(ld.plan_digest_fixes(memory))[f.name]
        assert "description" in fix.missing

    def test_complete_digest_untouched(self, ld, digest_brain):
        fixes = _by_name(ld.plan_digest_fixes(digest_brain))
        assert "2026-03-26__already-complete__cc33dd44.md" not in fixes

    def test_unparseable_frontmatter_skipped_with_reason(self, ld, digest_brain):
        """Guessing at a block YAML rejects risks corrupting it further.
        Report it and point at the tool that owns the fix."""
        fix = _by_name(ld.plan_digest_fixes(digest_brain))[
            "2026-03-27__unparseable__ee55ff66.md"]
        assert fix.skipped_reason
        assert "repair" in fix.skipped_reason

    def test_symlink_skipped(self, ld, tmp_path):
        memory = tmp_path / "memory"
        digests = memory / "semantic" / "digests"
        digests.mkdir(parents=True)
        outside = _write(tmp_path / "outside.md", _digest_text(_FM_LINES, _BODY))
        (digests / "linked.md").symlink_to(outside)
        assert "linked.md" not in _by_name(ld.plan_digest_fixes(memory))

    def test_plan_is_sorted_by_path(self, ld, digest_brain):
        fixes = ld.plan_digest_fixes(digest_brain)
        assert [f.file for f in fixes] == sorted(f.file for f in fixes)


class TestDeriveName:
    def test_name_from_filename_slug(self, ld, tmp_path):
        """Existing stems are already slug-shaped and must pass through
        byte-identical — the filename IS the stable identifier a wikilink
        would target."""
        stem = "2026-03-24__q2-2026-facilities-roadmap__ff1a201a"
        assert ld.derive_name(tmp_path / f"{stem}.md") == stem

    def test_name_slugifies_spaces_and_case(self, ld, tmp_path):
        assert ld.derive_name(tmp_path / "Weird Name (v2).md") == "weird-name-v2"

    def test_name_falls_back_when_slug_is_empty(self, ld, tmp_path):
        assert ld.derive_name(tmp_path / "--.md") == "digest"

    def test_name_is_length_capped(self, ld, tmp_path):
        assert len(ld.derive_name(tmp_path / ("x" * 400 + ".md"))) <= 120

    def test_proposed_name_matches_derive_name(self, ld, digest_brain):
        fix = _by_name(ld.plan_digest_fixes(digest_brain))[
            "2026-03-24__q2-2026-facilities-roadmap__ff1a201a.md"]
        assert fix.proposed["name"] == fix.file.stem


class TestDeriveDescription:
    def test_h1_plus_first_sentence(self, ld):
        desc = ld.derive_description(_BODY, fallback="fallback")
        assert desc.startswith(
            "Q2 2026 Facilities Roadmap Setup: Linear Projects, Gantt "
            "Visualization, and Capacity Planning")
        assert " — " in desc
        assert "Set up the complete Q2 2026 Facilities roadmap" in desc
        # Only the FIRST sentence of the paragraph.
        assert "Second sentence that must not appear" not in desc

    def test_capped_at_200_with_ellipsis_on_a_word_boundary(self, ld):
        body = (
            "# " + " ".join(["Roadmap"] * 20) + "\n\n"
            "## What you did\n\n"
            + " ".join(["planned"] * 60) + ".\n"
        )
        desc = ld.derive_description(body, fallback="fallback")
        assert len(desc) <= ld.DESCRIPTION_MAX == 200
        assert desc.endswith("…")
        # Truncation happens at a space, so the ellipsis never lands
        # mid-word or after a dangling space.
        assert not desc[:-1].endswith(" ")

    def test_skips_headings_bullets_and_placeholders(self, ld):
        body = (
            "# Only a heading\n\n"
            "## Decisions\n\n"
            "- first decision\n"
            "- second decision\n\n"
            "## Files touched\n\n"
            "_(none recorded)_\n\n"
            "> quoted aside\n\n"
            "```\ncode fence\n```\n\n"
            "| a | b |\n\n"
            "## What was learned\n\n"
            "The real prose paragraph starts here. Second sentence follows.\n"
        )
        desc = ld.derive_description(body, fallback="fallback")
        assert desc == "Only a heading — The real prose paragraph starts here."

    def test_h1_only_when_there_is_no_paragraph(self, ld):
        assert ld.derive_description("# Just a title\n", fallback="fb") == "Just a title"

    def test_paragraph_only_when_there_is_no_h1(self, ld):
        assert ld.derive_description("Some prose. More.\n", fallback="fb") == "Some prose."

    def test_fallback_when_body_is_empty(self, ld):
        assert ld.derive_description("   \n\n", fallback="a humanized stem") == \
            "a humanized stem"

    def test_whitespace_is_collapsed(self, ld):
        body = "# Title\n\nwrapped across\nseveral   lines here.\n"
        assert ld.derive_description(body, fallback="fb") == \
            "Title — wrapped across several lines here."

    def test_proposed_description_is_the_raw_value_not_yaml_quoted(
            self, ld, digest_brain):
        """`proposed` carries the VALUE; quoting happens at write time, so
        the JSON manifest shows what a human would read."""
        fix = _by_name(ld.plan_digest_fixes(digest_brain))[
            "2026-03-24__q2-2026-facilities-roadmap__ff1a201a.md"]
        assert not fix.proposed["description"].startswith('"')
        assert len(fix.proposed["description"]) <= ld.DESCRIPTION_MAX


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

class TestApply:
    def test_dry_run_writes_nothing(self, ld, digest_brain):
        target = (digest_brain / "semantic" / "digests"
                  / "2026-03-24__q2-2026-facilities-roadmap__ff1a201a.md")
        before = target.read_bytes()
        assert ld.apply_digest_fixes(ld.plan_digest_fixes(digest_brain)) == []
        assert target.read_bytes() == before

    def test_dry_run_is_the_default(self, ld, digest_brain):
        """`dry_run` defaults to True: writing to the user's only copy of
        a session must be an explicit act."""
        target = (digest_brain / "semantic" / "digests"
                  / "2026-03-25__crlf-session__aa11bb22.md")
        before = target.read_bytes()
        ld.apply_digest_fixes(ld.plan_digest_fixes(digest_brain))
        assert target.read_bytes() == before

    def test_apply_splices_into_existing_block_preserving_body(
            self, ld, digest_brain):
        target = (digest_brain / "semantic" / "digests"
                  / "2026-03-24__q2-2026-facilities-roadmap__ff1a201a.md")
        tail_before = _fm_tail(target)

        written = ld.apply_digest_fixes(
            ld.plan_digest_fixes(digest_brain), dry_run=False)
        assert target in written

        # Body bytes: untouched.
        assert _fm_tail(target) == tail_before
        # The three keys go in FIRST, ahead of the existing block.
        assert _fm_keys_in_order(target)[:3] == ["name", "description", "type"]
        # Every pre-existing key survives, with its value.
        fm = parse_path(target).frontmatter
        assert fm["session_id"] == "ff1a2018-0d7c-4b31-9c0e-3a1e2b4c5d6e"
        assert fm["domain_tags"] == ["facilities", "roadmap"]
        assert fm["salience"] == 8
        assert fm["outcome"] == "completed"
        # And the new keys parse back as real values.
        assert fm["type"] == "digest"
        assert fm["name"] == target.stem
        assert fm["description"].startswith("Q2 2026 Facilities Roadmap Setup")

    def test_apply_preserves_crlf(self, ld, digest_brain):
        target = (digest_brain / "semantic" / "digests"
                  / "2026-03-25__crlf-session__aa11bb22.md")
        tail_before = _fm_tail(target)
        ld.apply_digest_fixes(ld.plan_digest_fixes(digest_brain), dry_run=False)

        after = target.read_bytes().decode("utf-8")
        assert _fm_tail(target) == tail_before
        assert re.search(r"(?<!\r)\n", after) is None, \
            "splice introduced a bare LF into a CRLF digest"
        assert parse_path(target).frontmatter["type"] == "digest"

    def test_apply_leaves_lf_files_lf(self, ld, digest_brain):
        target = (digest_brain / "semantic" / "digests"
                  / "2026-03-24__q2-2026-facilities-roadmap__ff1a201a.md")
        ld.apply_digest_fixes(ld.plan_digest_fixes(digest_brain), dry_run=False)
        assert "\r" not in target.read_bytes().decode("utf-8")

    def test_description_is_written_yaml_quoted(self, ld, digest_brain):
        """Descriptions are free text and routinely contain a colon
        (`Roadmap Setup: Linear Projects`). Unquoted, that makes the whole
        block unparseable — the exact failure `--repair` cleans up."""
        target = (digest_brain / "semantic" / "digests"
                  / "2026-03-24__q2-2026-facilities-roadmap__ff1a201a.md")
        ld.apply_digest_fixes(ld.plan_digest_fixes(digest_brain), dry_run=False)
        raw = target.read_bytes().decode("utf-8")
        # Re-derive the value the same way the planner did (the file is now
        # complete, so there is no fix left to read it from).
        value = ld.derive_description(parse_path(target).body, fallback=target.stem)
        assert f"description: {lint._double_quote(value)}" in raw
        # Whole block still parses.
        assert lint.lint_file(
            target, known_keys=set(), kinds=frozenset({"unparseable_frontmatter"}),
            brain_root=digest_brain) == []

    def test_apply_is_idempotent(self, ld, digest_brain):
        ld.apply_digest_fixes(ld.plan_digest_fixes(digest_brain), dry_run=False)
        second = ld.plan_digest_fixes(digest_brain)
        # The unparseable digest is still reported (as skipped) but nothing
        # is proposed for any file.
        assert [f for f in second if f.proposed] == []
        target = (digest_brain / "semantic" / "digests"
                  / "2026-03-24__q2-2026-facilities-roadmap__ff1a201a.md")
        before = target.read_bytes()
        ld.apply_digest_fixes(second, dry_run=False)
        assert target.read_bytes() == before

    def test_apply_never_writes_a_skipped_digest(self, ld, digest_brain):
        target = (digest_brain / "semantic" / "digests"
                  / "2026-03-27__unparseable__ee55ff66.md")
        before = target.read_bytes()
        written = ld.apply_digest_fixes(
            ld.plan_digest_fixes(digest_brain), dry_run=False)
        assert target not in written
        assert target.read_bytes() == before

    def test_apply_does_not_fabricate_a_frontmatter_block_for_others(
            self, ld, digest_brain):
        """Only digests are in scope — a lesson elsewhere in the brain is
        never rewritten by this pass."""
        lesson = _write(digest_brain / "semantic" / "lessons" / "keep-me.md",
                        "---\nname: keep-me\n---\nbody\n")
        before = lesson.read_bytes()
        ld.apply_digest_fixes(ld.plan_digest_fixes(digest_brain), dry_run=False)
        assert lesson.read_bytes() == before


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

class TestManifest:
    def test_dry_run_manifest_text(self, ld, digest_brain):
        fixes = ld.plan_digest_fixes(digest_brain)
        out = ld.render_digest_manifest(fixes, digest_brain)
        assert out.startswith("== recall lint --fix-digests ==")
        assert "digests under" in out
        assert "semantic/digests/2026-03-24__q2-2026-facilities-roadmap__ff1a201a.md" in out
        assert "+ name: 2026-03-24__q2-2026-facilities-roadmap__ff1a201a" in out
        assert "+ description:" in out
        assert "+ type: digest" in out
        assert "file(s) would change" in out
        assert "--fix-digests --apply" in out

    def test_manifest_reports_the_skipped_digest(self, ld, digest_brain):
        out = ld.render_digest_manifest(ld.plan_digest_fixes(digest_brain), digest_brain)
        assert "2026-03-27__unparseable__ee55ff66.md" in out
        assert "skipped" in out.lower()

    def test_applied_manifest_reports_what_was_written(self, ld, digest_brain):
        fixes = ld.plan_digest_fixes(digest_brain)
        applied = ld.apply_digest_fixes(fixes, dry_run=False)
        out = ld.render_digest_manifest(fixes, digest_brain, applied=applied)
        assert f"Backfilled frontmatter on {len(applied)} digest(s)." in out

    def test_manifest_paths_are_relative_to_the_memory_root(self, ld, digest_brain):
        out = ld.render_digest_manifest(ld.plan_digest_fixes(digest_brain), digest_brain)
        assert str(digest_brain) not in out, "manifest leaks the absolute root"

    def test_json_manifest_field_names(self, ld):
        """The `--json` manifest is `[asdict(fix)]`, so the dataclass field
        names ARE the JSON contract."""
        names = [f.name for f in dataclasses.fields(ld.DigestFix)]
        assert names == ["file", "missing", "proposed", "skipped_reason"]

    def test_empty_plan_manifest_says_nothing_to_do(self, ld, tmp_path):
        out = ld.render_digest_manifest([], tmp_path)
        assert "0 file(s) would change" in out


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

class TestCli:
    def test_fix_digests_dry_run_exit_1_then_apply_exit_0(
            self, runner, isolated_xdg, clean_digest_brain):
        first = runner.invoke(
            app, ["lint", "--brain", str(clean_digest_brain), "--fix-digests"])
        assert first.exit_code == 1, first.output
        assert "== recall lint --fix-digests ==" in first.output
        assert "2 file(s) would change" in first.output
        # Dry run really was dry.
        assert parse_path(
            clean_digest_brain / "semantic" / "digests"
            / "2026-04-01__first-session__11112222.md").frontmatter.get("type") is None

        second = runner.invoke(
            app, ["lint", "--brain", str(clean_digest_brain),
                  "--fix-digests", "--apply"])
        assert second.exit_code == 0, second.output
        assert "Backfilled frontmatter on 2 digest(s)." in second.output

        for name in ("2026-04-01__first-session__11112222.md",
                     "2026-04-02__second-session__33334444.md"):
            fm = parse_path(
                clean_digest_brain / "semantic" / "digests" / name).frontmatter
            assert fm["type"] == "digest"
            assert fm["name"] == name[:-3]
            assert fm["description"]

        third = runner.invoke(
            app, ["lint", "--brain", str(clean_digest_brain), "--fix-digests"])
        assert third.exit_code == 0, third.output
        assert "0 file(s) would change" in third.output

    def test_fix_digests_json_manifest(self, runner, isolated_xdg, clean_digest_brain):
        result = runner.invoke(
            app, ["lint", "--brain", str(clean_digest_brain),
                  "--fix-digests", "--json"])
        assert result.exit_code == 1, result.output
        for key in ('"file"', '"missing"', '"proposed"', '"skipped_reason"'):
            assert key in result.output
        assert '"digest"' in result.output

    def test_lint_without_the_flag_does_not_touch_digests(
            self, runner, isolated_xdg, clean_digest_brain):
        target = (clean_digest_brain / "semantic" / "digests"
                  / "2026-04-01__first-session__11112222.md")
        before = target.read_bytes()
        result = runner.invoke(app, ["lint", "--brain", str(clean_digest_brain)])
        assert result.exit_code == 0, result.output
        assert target.read_bytes() == before
        assert "--fix-digests" not in result.output
