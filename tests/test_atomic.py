"""Tests for memory/_atomic.py — torn-write protection."""
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "memory"))

import _atomic  # noqa: E402


def test_atomic_write_text_creates_file(tmp_path):
    p = tmp_path / "out.txt"
    _atomic.atomic_write_text(p, "hello\n")
    assert p.read_text() == "hello\n"


def test_atomic_write_text_overwrites(tmp_path):
    p = tmp_path / "out.txt"
    p.write_text("old content\n")
    _atomic.atomic_write_text(p, "new content\n")
    assert p.read_text() == "new content\n"


def test_atomic_write_json(tmp_path):
    p = tmp_path / "data.json"
    _atomic.atomic_write_json(p, {"a": 1, "b": [1, 2, 3]})
    assert json.loads(p.read_text()) == {"a": 1, "b": [1, 2, 3]}


def test_atomic_write_creates_parent_dirs(tmp_path):
    p = tmp_path / "deep" / "nested" / "path" / "f.txt"
    _atomic.atomic_write_text(p, "ok")
    assert p.read_text() == "ok"


def test_no_temp_file_left_after_success(tmp_path):
    p = tmp_path / "out.txt"
    _atomic.atomic_write_text(p, "ok")
    siblings = list(tmp_path.iterdir())
    assert all(not s.name.endswith(".tmp") for s in siblings), (
        f"unexpected .tmp siblings: {siblings}"
    )


def test_replace_is_atomic_against_concurrent_reader(tmp_path):
    """A reader that opens the file before replace gets the OLD content
    (because replace points the directory entry at the new inode). The
    file's inode is intact; only future opens see the new data.
    """
    p = tmp_path / "f.txt"
    _atomic.atomic_write_text(p, "v1\n")
    fd_pre = p.open("rb")
    _atomic.atomic_write_text(p, "v2\n")
    pre_content = fd_pre.read()
    fd_pre.close()
    # Pre-replace fd held the v1 inode
    assert pre_content == b"v1\n"
    # New open sees v2
    assert p.read_text() == "v2\n"


def test_cleanup_stale_tmp(tmp_path):
    (tmp_path / "stale.tmp").write_text("crashed write")
    (tmp_path / "ok.json").write_text("{}")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "more.tmp").write_text("more crashed")

    removed = _atomic.cleanup_stale_tmp(tmp_path)
    assert removed == 2
    assert not (tmp_path / "stale.tmp").exists()
    assert not (sub / "more.tmp").exists()
    assert (tmp_path / "ok.json").exists()


def test_failed_write_cleans_temp(tmp_path, monkeypatch):
    """If atomic_write fails partway, no .tmp should be left behind."""
    p = tmp_path / "out.txt"

    # Force os.replace to raise so we can inspect cleanup
    real_replace = os.replace
    def boom(*args, **kwargs):
        raise OSError("simulated failure")
    monkeypatch.setattr(os, "replace", boom)

    try:
        _atomic.atomic_write_text(p, "data")
    except OSError:
        pass  # expected

    # Restore for the cleanup check
    monkeypatch.setattr(os, "replace", real_replace)

    siblings = list(tmp_path.iterdir())
    tmp_siblings = [s for s in siblings if s.name.endswith(".tmp")]
    assert not tmp_siblings, f"temp file leaked on failure: {tmp_siblings}"
