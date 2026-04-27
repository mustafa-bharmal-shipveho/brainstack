"""Tests for tools/redact.py — pre-commit secret scanner.

The redactor must:
  - flag known public token formats (AWS, GitHub, JWT, generic high-entropy)
  - exit non-zero on any hit (so pre-commit aborts the commit)
  - print <file>:<line>:<pattern_name> for each hit
  - respect a per-line allowlist (`# redact-allow: <reason>`)
  - skip binary files
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
REDACT_SCRIPT = REPO_ROOT / "agent" / "tools" / "redact.py"


def run_redact(target_dir: Path) -> subprocess.CompletedProcess:
    """Invoke redact.py on a directory; return the completed process."""
    return subprocess.run(
        [sys.executable, str(REDACT_SCRIPT), str(target_dir)],
        capture_output=True,
        text=True,
    )


def test_aws_access_key_blocks(tmp_path):
    f = tmp_path / "leak.txt"
    f.write_text("AWS_KEY=AKIAIOSFODNN7EXAMPLE\n")
    result = run_redact(tmp_path)
    assert result.returncode != 0
    assert "leak.txt" in result.stdout
    assert "aws_access_key" in result.stdout.lower() or "akia" in result.stdout.lower()


def test_github_pat_blocks(tmp_path):
    f = tmp_path / "config.py"
    f.write_text('TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"\n')
    result = run_redact(tmp_path)
    assert result.returncode != 0
    assert "config.py" in result.stdout


def test_github_oauth_token_blocks(tmp_path):
    f = tmp_path / "auth.txt"
    f.write_text("gho_abcdefghijklmnopqrstuvwxyz0123456789\n")
    result = run_redact(tmp_path)
    assert result.returncode != 0


def test_jwt_blocks(tmp_path):
    f = tmp_path / "token.txt"
    # Three-part JWT-shaped token
    f.write_text(
        "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c\n"
    )
    result = run_redact(tmp_path)
    assert result.returncode != 0


def test_generic_secret_pattern_blocks(tmp_path):
    f = tmp_path / "config.yml"
    f.write_text('api_key: "abc123def456ghi789jkl0mnop123qrs456"\n')
    result = run_redact(tmp_path)
    assert result.returncode != 0


def test_clean_file_passes(tmp_path):
    f = tmp_path / "readme.md"
    f.write_text("# A normal readme\n\nNothing secret here.\n")
    result = run_redact(tmp_path)
    assert result.returncode == 0


def test_allowlist_suppresses_match(tmp_path):
    f = tmp_path / "fixture.py"
    # Test fixture intentionally containing a sample-shape token, with allowlist marker
    f.write_text(
        '# redact-allow: example value used in test fixture\n'
        'EXAMPLE_KEY = "AKIAIOSFODNN7EXAMPLE"\n'
    )
    result = run_redact(tmp_path)
    assert result.returncode == 0, f"Allowlist should have suppressed match. stdout: {result.stdout}"


def test_multiple_files_scanned(tmp_path):
    (tmp_path / "a.txt").write_text("AKIAIOSFODNN7EXAMPLE\n")
    (tmp_path / "b.txt").write_text("ghp_abcdefghijklmnopqrstuvwxyz0123456789\n")
    result = run_redact(tmp_path)
    assert result.returncode != 0
    assert "a.txt" in result.stdout
    assert "b.txt" in result.stdout


def test_binary_file_skipped(tmp_path):
    # Embed a fake AWS key in a binary file — should be skipped
    f = tmp_path / "blob.bin"
    f.write_bytes(b"\x00\x01\x02AKIAIOSFODNN7EXAMPLE\x00\x03")
    result = run_redact(tmp_path)
    assert result.returncode == 0, f"Binary files must be skipped. stdout: {result.stdout}"


def test_output_format_includes_line_number(tmp_path):
    f = tmp_path / "leak.txt"
    f.write_text(
        "line one\n"
        "line two\n"
        "AKIAIOSFODNN7EXAMPLE on line three\n"
    )
    result = run_redact(tmp_path)
    assert result.returncode != 0
    # Format: <file>:<line>:<pattern>
    assert ":3:" in result.stdout, f"Expected line number 3 in output. stdout: {result.stdout}"
