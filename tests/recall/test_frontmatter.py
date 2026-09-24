"""Tests for YAML frontmatter parsing."""

from __future__ import annotations

import pytest

from recall.frontmatter import ParsedFile, parse_file_text, parse_path


class TestParseFileText:
    def test_well_formed(self):
        text = "---\nname: foo\ndescription: hello\ntype: feedback\n---\nbody text\n"
        parsed = parse_file_text(text)
        assert parsed.frontmatter == {
            "name": "foo",
            "description": "hello",
            "type": "feedback",
        }
        assert parsed.body == "body text\n"

    def test_no_frontmatter(self):
        text = "# Just a heading\n\nBody only.\n"
        parsed = parse_file_text(text)
        assert parsed.frontmatter == {}
        assert parsed.body == text

    def test_empty(self):
        parsed = parse_file_text("")
        assert parsed.frontmatter == {}
        assert parsed.body == ""

    def test_only_frontmatter_no_body(self):
        text = "---\nname: foo\n---\n"
        parsed = parse_file_text(text)
        assert parsed.frontmatter == {"name": "foo"}
        assert parsed.body == ""

    def test_unclosed_frontmatter_treated_as_body(self):
        # No closing --- means the whole thing is body. Don't crash.
        text = "---\nname: foo\n\nReal body but no closer.\n"
        parsed = parse_file_text(text)
        assert parsed.frontmatter == {}
        assert "Real body" in parsed.body

    def test_leading_blank_line_no_frontmatter(self):
        # Frontmatter MUST be on the first line. A leading blank disqualifies it.
        text = "\n---\nname: foo\n---\nbody\n"
        parsed = parse_file_text(text)
        assert parsed.frontmatter == {}

    def test_malformed_yaml_falls_back_to_body(self):
        # Tab in indentation — invalid YAML. Don't raise; degrade gracefully.
        text = "---\nname: bad\n\tdescription: tabs are bad\n---\nbody\n"
        parsed = parse_file_text(text)
        assert parsed.frontmatter == {}
        # Body should include the original raw text since parsing failed
        assert "tabs are bad" in parsed.body or parsed.body == ""

    def test_unicode_nfc(self):
        text = "---\nname: café\n---\nBody.\n"
        parsed = parse_file_text(text)
        assert parsed.frontmatter["name"] == "café"

    def test_unicode_nfd(self):
        # NFD: café = c + a + f + e + combining acute (0301)
        text = "---\nname: café\n---\nBody.\n"
        parsed = parse_file_text(text)
        # Whatever normalization we choose, parse must not crash
        assert "name" in parsed.frontmatter

    def test_extra_dashes_in_body_dont_split(self):
        text = (
            "---\n"
            "name: foo\n"
            "---\n"
            "Body has --- in it but that's fine.\n"
            "More body.\n"
        )
        parsed = parse_file_text(text)
        assert parsed.frontmatter == {"name": "foo"}
        assert "More body" in parsed.body

    def test_multiline_value(self):
        # YAML supports block scalars
        text = "---\nname: foo\ndescription: |\n  line one\n  line two\n---\nbody\n"
        parsed = parse_file_text(text)
        assert parsed.frontmatter["name"] == "foo"
        assert "line one" in parsed.frontmatter["description"]

    def test_list_value(self):
        text = "---\nname: foo\ntags:\n  - a\n  - b\n---\nbody\n"
        parsed = parse_file_text(text)
        assert parsed.frontmatter["tags"] == ["a", "b"]

    def test_numeric_and_bool_values(self):
        text = "---\nname: foo\nweight: 3\nactive: true\n---\nbody\n"
        parsed = parse_file_text(text)
        assert parsed.frontmatter["weight"] == 3
        assert parsed.frontmatter["active"] is True

    def test_crlf_line_endings(self):
        text = "---\r\nname: foo\r\n---\r\nbody\r\n"
        parsed = parse_file_text(text)
        assert parsed.frontmatter == {"name": "foo"}

    def test_bom_at_start(self):
        text = "﻿---\nname: foo\n---\nbody\n"
        parsed = parse_file_text(text)
        # BOM should be stripped — frontmatter still parses
        assert parsed.frontmatter == {"name": "foo"}


class TestParsePath:
    def test_real_file(self, auto_memory_brain):
        target = auto_memory_brain / "semantic/lessons/feedback_pin_dependencies.md"
        parsed = parse_path(target)
        assert parsed.frontmatter["name"] == "pin-dependencies"
        assert parsed.frontmatter["type"] == "feedback"

    def test_binary_file_does_not_raise(self, malformed_brain):
        target = malformed_brain / "binary.md"
        # Should not raise UnicodeDecodeError; should return something even if empty
        parsed = parse_path(target)
        assert isinstance(parsed, ParsedFile)

    def test_nonexistent_file_raises(self, tmp_path):
        with pytest.raises((FileNotFoundError, OSError)):
            parse_path(tmp_path / "missing.md")


class TestParseIsoDatetime:
    def test_none_returns_none(self):
        from recall.frontmatter import parse_iso_datetime
        assert parse_iso_datetime(None) is None

    def test_date_coerced_to_iso(self):
        import datetime

        from recall.frontmatter import parse_iso_datetime
        assert parse_iso_datetime(datetime.date(2026, 6, 1)) == "2026-06-01"

    def test_datetime_coerced_to_iso(self):
        import datetime

        from recall.frontmatter import parse_iso_datetime
        out = parse_iso_datetime(datetime.datetime(2026, 6, 1, 12, 30, 0))
        assert out == "2026-06-01T12:30:00"

    def test_valid_iso_string_passes_through(self):
        from recall.frontmatter import parse_iso_datetime
        assert parse_iso_datetime("2026-06-01T12:30:00Z") == "2026-06-01T12:30:00Z"
        assert parse_iso_datetime("2026-06-01") == "2026-06-01"

    def test_garbage_returns_none(self):
        from recall.frontmatter import parse_iso_datetime
        assert parse_iso_datetime("not a date") is None
        assert parse_iso_datetime("") is None
        assert parse_iso_datetime(12345) is None
        assert parse_iso_datetime(["2026-06-01"]) is None


class TestTemporalMeta:
    def test_none_frontmatter_defaults_current(self):
        from recall.frontmatter import temporal_meta
        meta = temporal_meta(None)
        assert meta.status == "current"
        assert meta.valid_from is None
        assert meta.valid_until is None
        assert meta.superseded_by is None
        assert meta.supersedes is None

    def test_empty_frontmatter_defaults_current(self):
        from recall.frontmatter import temporal_meta
        meta = temporal_meta({})
        assert meta.status == "current"
        assert meta.valid_from is None
        assert meta.superseded_by is None

    def test_needs_review_without_status_is_staged(self):
        from recall.frontmatter import temporal_meta
        assert temporal_meta({"needs_review": True}).status == "staged"

    def test_stance_superseded_alias(self):
        from recall.frontmatter import temporal_meta
        assert temporal_meta({"stance": "superseded"}).status == "superseded"

    def test_claim_stale_type_alias(self):
        from recall.frontmatter import temporal_meta
        assert temporal_meta({"type": "claim-stale"}).status == "superseded"

    def test_accepted_maps_to_current(self):
        from recall.frontmatter import temporal_meta
        assert temporal_meta({"status": "accepted"}).status == "current"

    def test_explicit_status_wins_over_needs_review(self):
        from recall.frontmatter import temporal_meta
        assert temporal_meta({"status": "current", "needs_review": True}).status == "current"

    def test_status_values(self):
        from recall.frontmatter import temporal_meta
        assert temporal_meta({"status": "current"}).status == "current"
        assert temporal_meta({"status": "staged"}).status == "staged"
        assert temporal_meta({"status": "provisional"}).status == "staged"
        assert temporal_meta({"status": "superseded"}).status == "superseded"
        assert temporal_meta({"status": "stale"}).status == "stale"
        assert temporal_meta({"status": "bogus"}).status == "unknown"

    def test_valid_from_missing_is_none(self):
        from recall.frontmatter import temporal_meta
        assert temporal_meta({"status": "current"}).valid_from is None

    def test_yaml_datetime_objects_coerced(self):
        from recall.frontmatter import parse_file_text, temporal_meta
        text = "---\nstatus: current\nvalid_from: 2026-06-01\n---\nbody\n"
        parsed = parse_file_text(text)
        meta = temporal_meta(parsed.frontmatter)
        assert meta.valid_from == "2026-06-01"
        assert meta.status == "current"

    def test_garbage_valid_from_is_none(self):
        from recall.frontmatter import temporal_meta
        meta = temporal_meta({"valid_from": "whenever"})
        assert meta.valid_from is None

    def test_supersession_links(self):
        from recall.frontmatter import temporal_meta
        meta = temporal_meta({
            "status": "superseded",
            "superseded_by": "lesson_new",
            "valid_until": "2026-06-01T00:00:00Z",
        })
        assert meta.superseded_by == "lesson_new"
        assert meta.valid_until == "2026-06-01T00:00:00Z"
        meta2 = temporal_meta({"supersedes": "lesson_old"})
        assert meta2.supersedes == "lesson_old"

    def test_never_raises_on_weird_types(self):
        from recall.frontmatter import temporal_meta
        meta = temporal_meta({"status": 42, "superseded_by": ["x"], "valid_until": {}})
        assert meta.status == "unknown"
        assert meta.superseded_by is None
        assert meta.valid_until is None
