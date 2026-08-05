"""Tests for the per-file secret-scan quarantine gate.

These lock in the invariants behind the sync.sh quarantine mechanism. The
originating bug: both secret gates failed closed AND globally, so one
unverified false positive stopped the entire brain from syncing for a
month, silently. Fixing that traded a hard block for a per-file skip —
which is only safe if the suppression rules below stay exactly as tight
as they are here.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "agent" / "tools"
sys.path.insert(0, str(TOOLS))

from redact import (  # noqa: E402
    SCAN_ALLOWLIST_FILENAME,
    is_false_positive,
    load_scan_allowlist,
    looks_like_vendor_credential,
)
import scan_gate  # noqa: E402


# A git SHA-1 and a 40-hex CircleCI/legacy-GitHub token are the same shape.
SHA40 = "d257a25ff5ac698ca3db046a09350cf1e15ca34b"
SHA256 = "bd58d886817f13730753bbb52995274f56d9ddef27401930543a166f05ed0dd0"
UUID = "239a12f6-1006-4e30-b1d6-afaa91877e99"


# ---------------------------------------------------------------- filters


class TestStructuralSuppressionRequiresVerification:
    """Shape-based suppression is only licensed by a conclusive verdict.

    trufflehog reports Verified=false both for "checked, invalid" and
    "could not check", and gitleaks never verifies at all. Suppressing on
    shape without a conclusive answer would wave through a real 40-hex
    credential as "just a git SHA".
    """

    @pytest.mark.parametrize("value", [SHA40, SHA256, UUID])
    def test_suppressed_when_verification_was_conclusive(self, value):
        assert is_false_positive(value, [], verification_conclusive=True)

    @pytest.mark.parametrize("value", [SHA40, SHA256, UUID])
    def test_quarantined_when_verification_was_not_conclusive(self, value):
        # gitleaks, or trufflehog with a VerificationError.
        assert is_false_positive(value, [], verification_conclusive=False) is None

    def test_gitleaks_findings_are_marked_inconclusive(self, tmp_path, monkeypatch):
        """Guards the wiring, not just the helper.

        gitleaks has no verification step, so every hit it produces must
        arrive with conclusive=False — otherwise a real 40-hex CircleCI
        token would be structurally suppressed as a git SHA.
        """
        report = f'[{{"RuleID":"generic","Secret":"{SHA40}",' \
                 f'"File":"memory/a.md","StartLine":3}}]'
        monkeypatch.setattr(
            scan_gate.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(
                a[0], 1, stdout=report, stderr=""
            ),
        )
        findings = scan_gate._run_gitleaks(tmp_path)
        assert len(findings) == 1
        assert findings[0]["conclusive"] is False
        assert findings[0]["verified"] is False
        # And therefore it survives filtering rather than being dropped.
        assert (
            is_false_positive(
                findings[0]["raw"], [],
                verification_conclusive=findings[0]["conclusive"],
            )
            is None
        )

    def test_trufflehog_verification_error_is_inconclusive(self, tmp_path, monkeypatch):
        """Verified=false + a VerificationError means 'could not check'."""
        import json

        line = json.dumps({
            "DetectorName": "Circle", "Verified": False,
            "VerificationError": "dial tcp: lookup circleci.com: no such host",
            "Raw": SHA40,
            "SourceMetadata": {"Data": {"Filesystem": {
                "file": "memory/a.md", "line": 1}}},
        })
        monkeypatch.setattr(
            scan_gate.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(
                a[0], 0, stdout=line, stderr=""
            ),
        )
        findings = scan_gate._run_trufflehog(tmp_path)
        assert findings[0]["conclusive"] is False


class TestVendorShapeBeatsPlaceholder:
    """A placeholder word must not excuse a real credential shape.

    AWS's published docs key ends in a literal "EXAMPLE"; without this
    rule the `example` marker would suppress it — and equally suppress a
    genuine AKIA key someone padded with the word.
    """

    @pytest.mark.parametrize(
        "value",
        [
            "AKIAIOSFODNN7EXAMPLE",
            "ASIAIOSFODNN7EXAMPLE",
            "pattern_AKIAIOSFODNN7EXAMPLE_other_stuff",
            "n2026-04-13__AKIAIOSFODNN7EXAMPLE-stuff__7a02e53e",
        ],
    )
    def test_vendor_shaped_values_are_not_suppressed(self, value):
        assert looks_like_vendor_credential(value)
        assert is_false_positive(value, [], verification_conclusive=True) is None

    @pytest.mark.parametrize(
        "value", ["u+your-pagerduty-api", "<YOUR_TOKEN>", "changeme", "xxxxxxxxxxxx"]
    )
    def test_plain_placeholders_are_suppressed(self, value):
        assert not looks_like_vendor_credential(value)
        assert is_false_positive(value, []) == "documentation placeholder"

    def test_allowlist_can_still_clear_a_vendor_shape(self):
        # Explicit user opt-in is the one thing that overrides it.
        allow = [re.compile(r"^AKIAIOSFODNN7EXAMPLE$")]
        assert is_false_positive("AKIAIOSFODNN7EXAMPLE", allow)


def _fixture(prefix: str, body: str) -> str:
    """Assemble a credential-shaped fixture at runtime.

    Never write a complete credential-shaped literal into this file.
    GitHub push protection scans the raw source text and blocks the push
    (it caught exactly this during review), and the repo's own redact.py
    pre-commit hook would flag it too. Splitting the value means the
    pattern only exists at runtime, where the assertions need it.
    """
    return prefix + body


REAL_SHAPED_FIXTURES = [
    _fixture("sk-ant-", "api03-AbC123dEf456GhI789jKlMnO0pQrStUvWxYz"),
    _fixture("xoxb-", "123456789012-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx"),
    _fixture("ghp_", "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"),
    _fixture("glpat-", "AbCdEfGhIjKlMnOpQrSt"),
    _fixture("AKIA", "Z4Q7RTBN3WKLM2PX"),
]


class TestRealCredentialsAreNeverSuppressed:
    @pytest.mark.parametrize("value", REAL_SHAPED_FIXTURES)
    def test_not_filtered(self, value):
        for conclusive in (True, False):
            assert (
                is_false_positive(value, [], verification_conclusive=conclusive)
                is None
            ), value

    @pytest.mark.parametrize("value", REAL_SHAPED_FIXTURES)
    def test_recognised_as_vendor_shaped(self, value):
        assert looks_like_vendor_credential(value), value


class TestVerifiedFindingsBypassAllFilters:
    def test_verified_hit_is_always_quarantined(self):
        # main() only consults is_false_positive when not verified; this
        # documents that contract so a refactor cannot quietly break it.
        src = (TOOLS / "scan_gate.py").read_text()
        assert 'if not f["verified"]:' in src


# ------------------------------------------------------------- allowlist


class TestAllowlist:
    def test_missing_file_is_empty_not_an_error(self, tmp_path):
        assert load_scan_allowlist(tmp_path) == []

    def test_comments_and_blanks_ignored(self, tmp_path):
        (tmp_path / SCAN_ALLOWLIST_FILENAME).write_text(
            "# a comment\n\n^1[A-Za-z0-9_-]{32}$\n"
        )
        assert len(load_scan_allowlist(tmp_path)) == 1

    def test_bad_regex_fails_open_and_keeps_the_rest(self, tmp_path):
        (tmp_path / SCAN_ALLOWLIST_FILENAME).write_text("^valid$\n[unclosed\n^also$\n")
        pats = load_scan_allowlist(tmp_path)
        assert [p.pattern for p in pats] == ["^valid$", "^also$"]

    def test_shared_by_both_gates(self):
        # scan_gate must re-export redact's loader, not define its own.
        assert scan_gate.load_allowlist is load_scan_allowlist
        assert scan_gate.is_false_positive is is_false_positive


# ------------------------------------------------------------------ paths


class TestPathNormalisation:
    def test_git_internals_are_never_quarantined(self, tmp_path):
        # Unstageable, and quarantining them inflates the count while
        # holding nothing back.
        assert scan_gate._rel(".git/objects/ab/cdef", tmp_path) is None
        assert scan_gate._rel(".git", tmp_path) is None

    def test_paths_outside_the_brain_are_dropped(self, tmp_path):
        assert scan_gate._rel("/etc/passwd", tmp_path) is None

    def test_relative_path_is_preserved(self, tmp_path):
        (tmp_path / "memory").mkdir()
        (tmp_path / "memory" / "a.md").write_text("x")
        assert scan_gate._rel("memory/a.md", tmp_path) == "memory/a.md"


# ----------------------------------------------------------- redact CLI


def _run_redact(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOLS / "redact.py"), *args],
        capture_output=True,
        text=True,
    )


class TestRedactCliAcceptsFiles:
    """The pre-commit hook passes staged files, not the whole tree."""

    def test_directory_argument_still_works(self, tmp_path):
        (tmp_path / "clean.md").write_text("nothing to see\n")
        assert _run_redact(str(tmp_path)).returncode == 0

    def test_single_file_argument(self, tmp_path):
        f = tmp_path / "leak.md"
        f.write_text("AWS_KEY=AKIAIOSFODNN7EXAMPLE\n")
        assert _run_redact(str(f)).returncode == 1

    def test_multiple_file_arguments(self, tmp_path):
        a, b = tmp_path / "a.md", tmp_path / "b.md"
        a.write_text("fine\n")
        b.write_text("AWS_KEY=AKIAIOSFODNN7EXAMPLE\n")
        assert _run_redact(str(a), str(b)).returncode == 1

    def test_vanished_path_is_tolerated_if_others_remain(self, tmp_path):
        a = tmp_path / "a.md"
        a.write_text("fine\n")
        # A staged path can disappear between `git diff --cached` and here.
        res = _run_redact(str(a), str(tmp_path / "gone.md"))
        assert res.returncode == 0

    def test_all_paths_missing_is_an_error(self, tmp_path):
        assert _run_redact(str(tmp_path / "gone.md")).returncode == 2
