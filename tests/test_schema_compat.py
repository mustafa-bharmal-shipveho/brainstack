"""Schema-compatibility tests against vendored upstream code.

Pinned upstream commit: df806abace1a693e042844bf4ac0cccf9bb6270a (v0.11.2)

These tests fail loudly if upstream schema drifts in a way that breaks our
lessons.jsonl extension (`why`, `how_to_apply`, `original_markdown_path`).
Run as part of every UPSTREAM.md rebase pass.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_MEMORY = REPO_ROOT / "agent" / "memory"
sys.path.insert(0, str(AGENT_MEMORY))

import render_lessons  # noqa: E402


def _make_test_brain(tmp_path: Path) -> Path:
    """Create a minimal ~/.agent/-shaped brain in tmp_path. Returns semantic dir."""
    semantic = tmp_path / "memory" / "semantic"
    semantic.mkdir(parents=True, exist_ok=True)
    return semantic


def test_lessons_schema_extension_renders_when_present(tmp_path):
    """Custom fields render under the bullet."""
    semantic = _make_test_brain(tmp_path)

    lesson = {
        "id": "lesson_abc123",
        "claim": "Always serialize timestamps in UTC",
        "conditions": ["timestamp", "utc"],
        "evidence_ids": ["2026-04-26T10:00:00"],
        "status": "accepted",
        "accepted_at": "2026-04-26T10:00:00",
        "reviewer": "host-agent",
        "rationale": "test",
        "cluster_size": 1,
        "canonical_salience": 8.0,
        "confidence": 0.5,
        "support_count": 0,
        "contradiction_count": 0,
        "supersedes": None,
        "source_candidate": "abc123",
        # extension fields:
        "why": "Cross-region comparisons silently produce off-by-N-hours bugs.",
        "how_to_apply": "Anywhere a timestamp leaves a service boundary.",
    }
    render_lessons.append_lesson(lesson, str(semantic))
    render_lessons.render_lessons(str(semantic))

    md = (semantic / "LESSONS.md").read_text()
    assert "Always serialize timestamps in UTC" in md
    # Extension fields render as italic (single-asterisk), NOT bold.
    # Bold + dash-prefixed indented bullets get misread as new top-level
    # bullets by migrate_legacy_bullets() — see render_lessons.py comment.
    assert "*Why:* Cross-region comparisons silently" in md
    assert "*How to apply:* Anywhere a timestamp leaves" in md
    # No leading dash on the extension lines (would trigger legacy migration)
    assert "- *Why:*" not in md
    assert "- *How to apply:*" not in md


def test_lessons_schema_backward_compatible(tmp_path):
    """Lesson without extension fields renders identically to upstream."""
    semantic = _make_test_brain(tmp_path)

    lesson = {
        "id": "lesson_xyz999",
        "claim": "Run tests against a real database",
        "conditions": ["test", "database"],
        "evidence_ids": ["2026-04-26T11:00:00"],
        "status": "accepted",
        "accepted_at": "2026-04-26T11:00:00",
        "reviewer": "host-agent",
        "rationale": "test",
        "cluster_size": 1,
        "canonical_salience": 7.0,
        "confidence": 0.5,
        "support_count": 0,
        "contradiction_count": 0,
        "supersedes": None,
        "source_candidate": "xyz999",
        # NO extension fields
    }
    render_lessons.append_lesson(lesson, str(semantic))
    render_lessons.render_lessons(str(semantic))

    md = (semantic / "LESSONS.md").read_text()
    # Bullet present
    assert "Run tests against a real database" in md
    # Nothing rendered for absent extensions
    assert "**Why:**" not in md
    assert "**How to apply:**" not in md


def test_lessons_jsonl_required_fields_match_upstream():
    """The set of required fields hasn't drifted from upstream's graduate.py."""
    # Upstream v0.11.2 graduate.py constructs lesson rows with these fields.
    # If a rebase-pass adds/removes a required field, this test must be
    # updated together with NOTICE / UPSTREAM.md.
    expected_fields = {
        "id", "claim", "conditions", "evidence_ids",
        "status", "accepted_at", "reviewer", "rationale",
        "cluster_size", "canonical_salience", "confidence",
        "support_count", "contradiction_count",
        "supersedes", "source_candidate",
    }
    schema_path = REPO_ROOT / "schemas" / "lessons.schema.json"
    schema = json.loads(schema_path.read_text())
    declared_fields = set(schema["properties"].keys())
    extension_fields = {
        "why", "how_to_apply", "original_markdown_path",
        # Migration-extension fields added when migrating native auto-memory
        # dirs into the brain. See CHANGELOG / lessons.schema.json.
        "name", "type", "source_session_id",
    }
    upstream_in_schema = declared_fields - extension_fields
    assert upstream_in_schema == expected_fields, (
        f"Schema drift: missing={expected_fields - upstream_in_schema} "
        f"extra={upstream_in_schema - expected_fields}"
    )


def test_extension_fields_documented_as_extensions():
    """Extension fields' descriptions explicitly mark them as our additions."""
    schema_path = REPO_ROOT / "schemas" / "lessons.schema.json"
    schema = json.loads(schema_path.read_text())
    for fname in ["why", "how_to_apply", "original_markdown_path"]:
        desc = schema["properties"][fname]["description"]
        assert "[brainstack extension]" in desc, (
            f"Extension field {fname} not marked as extension in schema description"
        )
    # Migration-extension fields use a slightly different tag.
    for fname in ["name", "type", "source_session_id"]:
        desc = schema["properties"][fname]["description"]
        assert "[brainstack migration extension]" in desc, (
            f"Migration field {fname} not marked as extension in schema description"
        )
