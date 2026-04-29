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
