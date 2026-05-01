"""Phase 3a: runtime/core/locking.py contract tests.

Ports the harness's _atomic_append.py pattern into reusable runtime code.
The contract: locked_append(path, line) is atomic across concurrent processes
and threads. Sentinel lock pattern (lock != data file) for compatibility
with atomic-replace.
"""
from __future__ import annotations

import concurrent.futures
import json
from pathlib import Path

import pytest

from runtime.core.locking import (
    locked_append,
    locked_write,
    sentinel_lock_path,
)


def test_sentinel_lock_path_is_distinct_from_data() -> None:
    p = Path("/tmp/x/events.jsonl")
    assert sentinel_lock_path(p) != p


def test_locked_append_creates_file_and_lock(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    locked_append(log, "line1\n")
    assert log.exists()
    assert sentinel_lock_path(log).exists()
    assert log.read_text() == "line1\n"


def test_locked_append_concurrent_no_corruption(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    n = 30

    def worker(i: int) -> None:
        locked_append(log, json.dumps({"i": i, "marker": f"m-{i:03d}"}) + "\n")

    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(worker, range(n)))

    lines = log.read_text().strip().splitlines()
    assert len(lines) == n
    parsed = sorted(json.loads(line)["i"] for line in lines)
    assert parsed == list(range(n))


def test_locked_append_appends_newline_if_missing(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    locked_append(log, "no newline")
    locked_append(log, "another no newline")
    lines = log.read_text().splitlines()
    assert lines == ["no newline", "another no newline"]


def test_locked_write_replaces_atomically(tmp_path: Path) -> None:
    """locked_write replaces a file atomically (write to .tmp, then rename)."""
    target = tmp_path / "manifest.json"
    locked_write(target, '{"version": 1}')
    assert target.read_text() == '{"version": 1}'
    locked_write(target, '{"version": 2}')
    assert target.read_text() == '{"version": 2}'


def test_locked_write_concurrent_last_writer_wins_no_torn(tmp_path: Path) -> None:
    target = tmp_path / "manifest.json"
    n = 20

    def worker(i: int) -> None:
        locked_write(target, json.dumps({"writer": i}))

    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(worker, range(n)))

    # File must contain exactly one valid JSON document, no torn writes.
    parsed = json.loads(target.read_text())
    assert "writer" in parsed
    assert 0 <= parsed["writer"] < n


def test_locked_append_creates_parent_dirs(tmp_path: Path) -> None:
    log = tmp_path / "deep" / "nested" / "dir" / "events.jsonl"
    locked_append(log, "ok\n")
    assert log.exists()


@pytest.mark.parametrize("encoding", ["utf-8", "ascii"])
def test_locked_append_handles_unicode(tmp_path: Path, encoding: str) -> None:
    log = tmp_path / "events.jsonl"
    locked_append(log, "résumé café 日本語\n")
    content = log.read_text(encoding="utf-8")
    assert "résumé" in content
