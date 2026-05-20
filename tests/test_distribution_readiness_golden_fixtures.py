"""Pre-distribution: the three golden JSONL fixtures must not contain the
maintainer's home directory OR company-specific terminology.

The audit (PR #43 follow-up) caught:

  B4 — `tests/recall/golden/real_brain_v{1,v2_hard,v2_hard_expanded}.jsonl`
       had 116 absolute paths under `/Users/mustafa.bharmal/`. Tests using
       these fail on any other machine. They also embed Veho-specific
       terms (ATL01, DEN-01, DTW, PKG_, "Middle Mile Sort", PSI, SEV-1,
       `shipveho-slack` URLs) that don't generalize and expose internal
       domain knowledge.

These tests pin both as a regression gate.

Acceptable fixture content:
  - Relative paths in `expected_doc` (e.g. `memory/semantic/digests/...`)
  - NO `expected_doc_abs` field — or if present, it must use a
    `${BRAIN_ROOT}` / `${HOME}` template so it's portable
  - Generic queries that don't name internal services / facilities /
    customer-specific identifiers
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = REPO_ROOT / "tests" / "recall" / "golden"

GOLDEN_FILES = [
    "real_brain_v1.jsonl",
    "real_brain_v2_hard.jsonl",
    "real_brain_v2_hard_expanded.jsonl",
]


# Personal-identifier patterns. Empty match → fail. Each entry is
# (pattern, human-readable name).
PERSONAL_PATTERNS = [
    (re.compile(r"/Users/mustafa[^/\"\s]*", re.IGNORECASE), "/Users/mustafa* absolute path"),
    (re.compile(r"\bmustafa[\.\-_]?bharmal\b", re.IGNORECASE), "mustafa.bharmal identifier"),
    (re.compile(r"\bshipveho\b", re.IGNORECASE), "shipveho (employer) reference"),
    (re.compile(r"https?://[^\"\s]*\bshipveho\b[^\"\s]*", re.IGNORECASE), "shipveho URL"),
]

# Veho-internal terminology. Each entry is (pattern, human-readable name).
# These are domain terms (facility codes, workflow names) that don't
# generalize beyond Veho and signal internal IP.
VEHO_TERMS = [
    (re.compile(r"\b(ATL|DEN|DTW|JFK|LAX|MIA|ORD|SFO)\d{2}\b"), "facility code (e.g. ATL01)"),
    (re.compile(r"\bPSI\s+#?[CU][0-9A-Z]+", re.IGNORECASE), "PSI Slack-channel-id reference"),
    (re.compile(r"\bMiddle Mile Sort\b", re.IGNORECASE), "Middle Mile Sort (internal workflow name)"),
    (re.compile(r"\bPending Build\b", re.IGNORECASE), "Pending Build (internal workflow name)"),
    (re.compile(r"\bSEV-\d\b"), "SEV-N severity tag (Veho convention)"),
    (re.compile(r"\bRack Recon\w*", re.IGNORECASE), "Rack Reconciliation reference"),
    (re.compile(r"\bLMSC\b"), "LMSC (Last-Mile Sort Center, internal)"),
    (re.compile(r"\bRFR\b"), "RFR (internal status code)"),
]


class TestGoldenFixturesHaveNoPersonalIdentifiers:
    """Test data that's distributed must not name the maintainer or
    their employer — neither in paths nor in body text."""

    @pytest.mark.parametrize("filename", GOLDEN_FILES)
    def test_no_personal_identifiers(self, filename: str):
        path = GOLDEN_DIR / filename
        assert path.is_file(), f"missing fixture: {path}"
        text = path.read_text()

        hits: list[str] = []
        for pattern, label in PERSONAL_PATTERNS:
            for match in pattern.finditer(text):
                # Find which line the hit is on for actionable error
                line_no = text.count("\n", 0, match.start()) + 1
                hits.append(f"  line {line_no}: {label} → {match.group(0)!r}")

        assert not hits, (
            f"{filename} contains personal identifiers — must be regenerated "
            f"with generic placeholders before distribution.\n" + "\n".join(hits)
        )


class TestGoldenFixturesHaveNoAbsolutePaths:
    """Each row's `expected_doc_abs` field (if present) must NOT be a
    hardcoded local path. Either drop the field entirely (tests use
    `expected_doc` relative paths) OR use a `${BRAIN_ROOT}` / `${HOME}`
    template that resolves at test runtime."""

    @pytest.mark.parametrize("filename", GOLDEN_FILES)
    def test_no_hardcoded_user_directory(self, filename: str):
        path = GOLDEN_DIR / filename
        offenders: list[str] = []
        for line_no, line in enumerate(path.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            abs_field = row.get("expected_doc_abs")
            if abs_field is None:
                continue  # field dropped — that's fine
            if abs_field.startswith("/Users/") or abs_field.startswith("/home/"):
                offenders.append(f"  line {line_no}: {abs_field}")
        assert not offenders, (
            f"{filename} has hardcoded user-directory absolute paths in "
            f"`expected_doc_abs`. Drop the field or template it.\n"
            + "\n".join(offenders)
        )


class TestGoldenFixturesAvoidVehoInternalTerminology:
    """Queries + expected_doc names should not embed Veho-internal domain
    terms. A colleague outside Mustafa's immediate team may not recognize
    these, and they signal internal IP."""

    @pytest.mark.parametrize("filename", GOLDEN_FILES)
    def test_no_veho_specific_terms(self, filename: str):
        path = GOLDEN_DIR / filename
        text = path.read_text()

        hits: list[str] = []
        for pattern, label in VEHO_TERMS:
            for match in pattern.finditer(text):
                line_no = text.count("\n", 0, match.start()) + 1
                hits.append(f"  line {line_no}: {label} → {match.group(0)!r}")

        assert not hits, (
            f"{filename} contains Veho-internal terminology. Replace with "
            f"generic placeholders before distribution.\n" + "\n".join(hits)
        )


class TestGoldenFixturesStillUsable:
    """After scrubbing, the files must still be valid JSONL with the right
    schema — the eval scripts depend on `query` + `expected_doc` fields."""

    @pytest.mark.parametrize("filename", GOLDEN_FILES)
    def test_jsonl_schema_intact(self, filename: str):
        path = GOLDEN_DIR / filename
        rows: list[dict] = []
        for line_no, line in enumerate(path.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                pytest.fail(f"{filename}:{line_no} invalid JSON: {e}")
            rows.append(row)

        assert rows, f"{filename} is empty — regen must produce ≥1 row"
        for i, row in enumerate(rows, start=1):
            assert "query" in row, f"{filename} row {i} missing `query` field"
            assert "expected_doc" in row, (
                f"{filename} row {i} missing `expected_doc` field"
            )
            # expected_doc should be a relative path
            assert not row["expected_doc"].startswith("/"), (
                f"{filename} row {i}: `expected_doc` should be relative, "
                f"got absolute: {row['expected_doc']}"
            )
