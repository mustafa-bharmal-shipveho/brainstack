"""Tests for the shared write-path redaction seam (TDD red phase).

Planned production module: `agent/tools/_redact_common.py` exposing
`redact_for_write(text, brain_root)`. It applies the builtin redaction
patterns PLUS private patterns from `<brain_root>/redact-private.txt`,
failing OPEN with a stderr WARN when a private pattern line is malformed
(a user typo must never make the adapters silently stop importing, and
must never disable the builtin coverage).

Seam contract (test the seams, not just the units): the codex and cursor
migrate adapters must pass episode/plan text through `redact_for_write`
BEFORE writing anything into the brain. Verified end-to-end here by
running `dispatch()` against synthetic sources seeded with a private
token and asserting the token appears NOWHERE in the written output.

Fixtures use placeholder identifiers only (Acme, Alice,
EXAMPLE-CUST-123456). `_redact_common` does not exist yet, so its import
is LAZY (inside test bodies) and collection never breaks.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "tools"))
sys.path.insert(0, str(REPO_ROOT / "agent" / "memory"))

from migrate_dispatcher import dispatch  # noqa: E402


PRIVATE_PATTERN = "EXAMPLE-CUST-[0-9]{6}"
CUST_TOKEN = "EXAMPLE-CUST-123456"
# Canonical AWS docs placeholder key. Matched by the builtin
# `aws_access_key` pattern, so it proves builtin coverage end-to-end.
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"  # redact-allow: canonical AWS docs example key, test fixture


@pytest.fixture
def brain_root(tmp_path: Path) -> Path:
    """A tmp brain root with a one-line redact-private.txt."""
    root = tmp_path / "brain"
    root.mkdir()
    (root / "redact-private.txt").write_text(PRIVATE_PATTERN + "\n")
    return root


# ---------------------------------------------------------------------------
# redact_for_write unit contract
# ---------------------------------------------------------------------------


class TestRedactForWrite:
    def test_private_pattern_applied_and_idempotent(self, brain_root: Path):
        from _redact_common import redact_for_write
        text = f"Alice escalated customer ref {CUST_TOKEN} for Acme"
        out = redact_for_write(text, brain_root)
        assert CUST_TOKEN not in out
        # Surrounding text survives; redaction is surgical.
        assert "Alice escalated customer ref" in out
        assert "for Acme" in out
        # Idempotent: a second pass over already-redacted text is a no-op.
        assert redact_for_write(out, brain_root) == out

    def test_malformed_pattern_line_warns_and_keeps_builtins(
        self, tmp_path: Path, capsys
    ):
        """A malformed regex line must fail OPEN: stderr WARN, builtin
        patterns still applied. Never fail closed (skip everything) and
        never crash."""
        from _redact_common import redact_for_write
        root = tmp_path / "brain"
        root.mkdir()
        (root / "redact-private.txt").write_text("([unclosed\n")
        out = redact_for_write(f"deploy key {AWS_KEY} leaked in Acme logs", root)
        # Builtins still redact the AWS-shaped key.
        assert AWS_KEY not in out
        err = capsys.readouterr().err
        assert err.strip(), "expected a stderr WARN for the malformed pattern line"
        low = err.lower()
        assert "warn" in low or "invalid" in low or "skipped" in low, (
            f"stderr should identify the malformed pattern; got: {err!r}"
        )

    def test_missing_private_file_is_builtin_only_and_silent(
        self, tmp_path: Path, capsys
    ):
        from _redact_common import redact_for_write
        root = tmp_path / "brain"
        root.mkdir()  # no redact-private.txt
        out = redact_for_write(f"{AWS_KEY} and {CUST_TOKEN}", root)
        # Builtin coverage still applies.
        assert AWS_KEY not in out
        # No private pattern loaded, so the org-specific token survives.
        assert CUST_TOKEN in out
        # And a missing optional file is NOT a warning condition.
        assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# Adapter end-to-end seams: token must appear NOWHERE in written output
# ---------------------------------------------------------------------------


# Minimal codex fixture shapes, copied from tests/test_codex_adapter.py.
def _make_codex_source_with_token(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    # Codex CLI's signature combo: history.jsonl + config.toml
    (root / "config.toml").write_text("# fake config\n")
    rollout = root / "sessions" / "2026" / "06" / "01" / "rollout-x.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout_lines = [
        {
            "type": "session_meta",
            "timestamp": "2026-06-01T10:00:00Z",
            "payload": {
                "id": "019dded3-2c2b-77d0-b6bf-545c92cdd4ad",
                "cli_version": "0.125.0",
                "model_provider": "openai",
                "cwd": "/home/alice/repo",
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-06-01T10:00:05Z",
            "payload": {
                "role": "user",
                "content": f"Look up {CUST_TOKEN} in the Acme queue",
                "type": "message",
            },
        },
    ]
    rollout.write_text("\n".join(json.dumps(l) for l in rollout_lines) + "\n")
    (root / "history.jsonl").write_text(
        json.dumps({
            "session_id": "019dded3-2c2b-77d0-b6bf-545c92cdd4ad",
            "text": f"investigate {CUST_TOKEN}",
            "ts": 1776965620,
        }) + "\n"
    )
    return root


class TestAdapterWritePaths:
    def test_codex_adapter_never_writes_private_token(
        self, tmp_path: Path, brain_root: Path
    ):
        """End-to-end: a codex source seeded with the private token is
        migrated into a brain whose redact-private.txt matches it. The
        token must appear NOWHERE in any written JSONL."""
        src = _make_codex_source_with_token(tmp_path / "codex")
        result = dispatch(src=src, dst=brain_root, dry_run=False)
        assert result.format == "codex-cli"
        assert result.tool_specific.get("episodes_imported", 0) >= 3

        epi_dir = brain_root / "memory" / "episodic" / "codex"
        written = sorted(epi_dir.rglob("*.jsonl"))
        assert written, f"no JSONL written under {epi_dir}"
        for f in written:
            content = f.read_text()
            assert CUST_TOKEN not in content, (
                f"{f} leaked the private customer token into the brain"
            )
        # The episodes themselves were still imported (redacted, not dropped).
        learnings = epi_dir / "AGENT_LEARNINGS.jsonl"
        rows = [json.loads(l) for l in learnings.read_text().strip().splitlines()]
        assert any("Acme queue" in r.get("detail", "") for r in rows), (
            "redaction should scrub the token, not drop the episode text"
        )

    def test_cursor_adapter_never_writes_private_token(
        self, tmp_path: Path, brain_root: Path
    ):
        """Same seam for cursor plans: plan text passes through
        redact_for_write before landing in personal/notes/cursor/."""
        src = tmp_path / "plans"
        src.mkdir()
        plan = (
            "---\n"
            "name: Handle Acme escalation\n"
            "overview: synthetic plan for redaction testing\n"
            "---\n"
            "\n"
            "# Plan\n"
            "\n"
            f"Alice flagged customer {CUST_TOKEN} in the Acme queue.\n"
        )
        (src / "acme-escalation_a1b2.plan.md").write_text(plan)

        result = dispatch(src=src, dst=brain_root, dry_run=False)
        assert result.format == "cursor-plans"
        assert result.files_written == 1

        cursor_dir = brain_root / "memory" / "personal" / "notes" / "cursor"
        files = sorted(cursor_dir.glob("*.plan.md"))
        assert files, f"no plan written under {cursor_dir}"
        for f in files:
            content = f.read_text()
            assert CUST_TOKEN not in content, (
                f"{f} leaked the private customer token into the brain"
            )
            # The rest of the plan still round-trips.
            assert "Handle Acme escalation" in content
            assert "Alice flagged customer" in content
