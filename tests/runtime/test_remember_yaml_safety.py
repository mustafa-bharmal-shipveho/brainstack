"""Regression: `recall remember` must produce parseable YAML frontmatter even
when the lesson text/description contains a colon.

Found by the clean-room end-to-end test: a lesson remembered with text like
"End-to-end test: verify X" wrote `description: End-to-end test: verify X`,
which is invalid YAML (the embedded ": " reads as a nested mapping). Every
frontmatter parser then returned {}, so the lesson's own needs_review,
provenance, and reviewed_by fields became invisible (e.g. `recall trace`
reported "provenance: none" for a fully-stamped lesson).
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _brain(tmp_path: Path) -> Path:
    root = tmp_path / ".agent"
    (root / "memory" / "semantic" / "lessons").mkdir(parents=True)
    return root


@pytest.mark.parametrize("text", [
    "fix: always quote yaml values",
    "End-to-end test: verify the thing works",
    'a description with "quotes" and a: colon',
    "trailing colon:",
    "url-ish thing http://example.com: see notes",
])
def test_remembered_frontmatter_is_parseable_yaml(tmp_path, text):
    from recall.frontmatter import parse_path
    from recall.remember import write_lesson

    root = _brain(tmp_path)
    path = write_lesson(text, brain_root=root, name="yaml-safety", reviewed=True)

    parsed = parse_path(Path(path))
    # The frontmatter must be non-empty and carry the stamped fields, i.e. the
    # embedded colon did not corrupt the YAML into an empty dict.
    fm = parsed.frontmatter
    assert fm, f"frontmatter parsed empty for text={text!r} (invalid YAML)"
    assert fm.get("source") == "recall-remember"
    assert fm.get("reviewed_by") == "human-cli"
    assert fm.get("description"), "description missing after parse"


def test_staged_lesson_with_colon_keeps_needs_review_visible(tmp_path):
    from recall.core import _is_needs_review, Document
    from recall.frontmatter import parse_path
    from recall.remember import write_lesson

    root = _brain(tmp_path)
    path = write_lesson("note: this should stage for review", brain_root=root, name="staged-colon")
    parsed = parse_path(Path(path))
    assert parsed.frontmatter.get("needs_review") in (True, "true"), (
        "needs_review must survive YAML parsing for a colon-containing lesson"
    )
    # And the retriever's needs_review detection must see it.
    doc = Document(path=str(path), source="brain", title="t",
                   frontmatter=parsed.frontmatter, body=parsed.body, text=parsed.body)
    assert _is_needs_review(doc) is True
