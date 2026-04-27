"""Tests for tools/redact_jsonl.py — sync-time JSONL scrubber.

The scrubber must:
  - replace secret-shaped substrings inside string fields with [REDACTED:<name>]
  - preserve non-string fields (numbers, booleans, nulls)
  - recursively walk lists and nested objects
  - leave clean files untouched
  - exit 1 (CI-friendly) when changes were applied
  - rewrite atomically — no torn file on crash
  - tolerate malformed JSON lines without crashing
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "agent" / "tools" / "redact_jsonl.py"


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
    )


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_clean_jsonl_untouched(tmp_path):
    f = tmp_path / "log.jsonl"
    write_jsonl(f, [{"msg": "all good"}, {"event": "ok", "n": 42}])
    before = f.read_text()
    result = run(str(f))
    assert result.returncode == 0
    assert f.read_text() == before


def test_aws_key_in_string_field_redacted(tmp_path):
    f = tmp_path / "log.jsonl"
    write_jsonl(f, [{"cmd": "export AWS_KEY=AKIAIOSFODNN7EXAMPLE"}])
    result = run(str(f))
    assert result.returncode == 1
    rows = read_jsonl(f)
    assert "AKIAIOSFODNN7EXAMPLE" not in rows[0]["cmd"]
    assert "[REDACTED:" in rows[0]["cmd"]


def test_github_token_in_nested_field_redacted(tmp_path):
    f = tmp_path / "log.jsonl"
    write_jsonl(f, [{
        "tool": "Bash",
        "input": {"command": "curl -H 'Authorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz0123456789'"},
    }])
    result = run(str(f))
    assert result.returncode == 1
    rows = read_jsonl(f)
    assert "ghp_" not in rows[0]["input"]["command"]


def test_jsonl_with_secret_in_array_redacted(tmp_path):
    f = tmp_path / "log.jsonl"
    write_jsonl(f, [{"args": ["--token", "ghp_abcdefghijklmnopqrstuvwxyz0123456789"]}])
    result = run(str(f))
    assert result.returncode == 1
    rows = read_jsonl(f)
    assert all("ghp_" not in s for s in rows[0]["args"])


def test_dry_run_does_not_write(tmp_path):
    f = tmp_path / "log.jsonl"
    write_jsonl(f, [{"cmd": "AKIAIOSFODNN7EXAMPLE"}])
    before = f.read_text()
    result = run("--dry-run", str(f))
    assert result.returncode == 1  # would change
    assert f.read_text() == before  # untouched


def test_directory_recursive(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    f1 = tmp_path / "a.jsonl"
    f2 = sub / "b.jsonl"
    write_jsonl(f1, [{"clean": "ok"}])
    write_jsonl(f2, [{"leak": "AKIAIOSFODNN7EXAMPLE"}])
    result = run(str(tmp_path))
    assert result.returncode == 1
    rows = read_jsonl(f2)
    assert "AKIAIOSFODNN7EXAMPLE" not in rows[0]["leak"]
    # f1 untouched
    rows1 = read_jsonl(f1)
    assert rows1[0]["clean"] == "ok"


def test_idempotent_after_first_run(tmp_path):
    f = tmp_path / "log.jsonl"
    write_jsonl(f, [{"cmd": "AKIAIOSFODNN7EXAMPLE"}])
    r1 = run(str(f))
    assert r1.returncode == 1
    r2 = run(str(f))
    assert r2.returncode == 0  # nothing left to change


def test_malformed_line_kept_verbatim(tmp_path):
    f = tmp_path / "log.jsonl"
    f.write_text(
        '{"good": "line"}\n'
        'this is not json\n'
        '{"good": "line2"}\n'
    )
    result = run(str(f))
    assert result.returncode == 0  # nothing scrubbed; malformed line kept
    assert "this is not json" in f.read_text()
    assert "malformed" in result.stderr.lower()


def test_non_string_fields_preserved(tmp_path):
    f = tmp_path / "log.jsonl"
    write_jsonl(f, [{
        "n": 42,
        "ok": True,
        "nada": None,
        "leaked": "AKIAIOSFODNN7EXAMPLE",
    }])
    result = run(str(f))
    assert result.returncode == 1
    rows = read_jsonl(f)
    assert rows[0]["n"] == 42
    assert rows[0]["ok"] is True
    assert rows[0]["nada"] is None
    assert "[REDACTED:" in rows[0]["leaked"]


def test_atomic_write_no_temp_left_behind(tmp_path):
    f = tmp_path / "log.jsonl"
    write_jsonl(f, [{"cmd": "AKIAIOSFODNN7EXAMPLE"}])
    run(str(f))
    # No leftover .tmp file
    assert not (tmp_path / "log.jsonl.tmp").exists()


def test_pem_block_scrubbed(tmp_path):
    f = tmp_path / "log.jsonl"
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\\n"
        "MIIEpAIBAAKCAQEA1234567890abcdef\\n"
        "-----END RSA PRIVATE KEY-----"
    )
    write_jsonl(f, [{"key": pem.replace("\\n", "\n")}])
    result = run(str(f))
    assert result.returncode == 1
    rows = read_jsonl(f)
    assert "BEGIN RSA PRIVATE KEY" not in rows[0]["key"]
