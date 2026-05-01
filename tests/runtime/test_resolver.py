"""v0.3.1: query resolver tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from runtime.adapters.claude_code.resolver import (
    resolve_brain_path,
    resolve_item,
)
from runtime.core.manifest import (
    SCHEMA_VERSION,
    InjectionItemSnapshot,
    Manifest,
)


def _manifest(items: list[InjectionItemSnapshot]) -> Manifest:
    return Manifest(
        schema_version=SCHEMA_VERSION,
        turn=0, ts_ms=0, session_id="t",
        budget_total=0, budget_used=0,
        items=items,
    )


def _snap(snap_id: str, source_path: str) -> InjectionItemSnapshot:
    return InjectionItemSnapshot(
        id=snap_id, bucket="hot", source_path=source_path,
        sha256="0" * 64, token_count=100, retrieval_reason="r",
        last_touched_turn=0, pinned=False, score=0.0,
    )


# ---------- resolve_item ----------

def test_resolve_empty_query() -> None:
    r = resolve_item("", _manifest([_snap("c-1", "a.md")]))
    assert r.match is None
    assert r.level == "empty"


def test_resolve_exact_id_wins() -> None:
    items = [_snap("c-77ab19d3", "p.md"), _snap("c-77abff", "q.md")]
    r = resolve_item("c-77ab19d3", _manifest(items))
    assert r.match == "c-77ab19d3"
    assert r.level == "exact-id"


def test_resolve_id_prefix_unique() -> None:
    items = [_snap("c-aaaa1", "a.md"), _snap("c-bbbb2", "b.md")]
    r = resolve_item("c-aaa", _manifest(items))
    assert r.match == "c-aaaa1"
    assert r.level == "id-prefix"


def test_resolve_id_prefix_ambiguous_returns_candidates() -> None:
    items = [_snap("c-aaaa1", "a.md"), _snap("c-aaaa2", "b.md")]
    r = resolve_item("c-aaa", _manifest(items))
    assert r.match is None
    assert sorted(r.candidates) == ["c-aaaa1", "c-aaaa2"]
    assert r.level == "id-prefix"


def test_resolve_basename_match() -> None:
    items = [
        _snap("c-1", "/path/to/postgres-locking.md"),
        _snap("c-2", "/path/to/other.md"),
    ]
    r = resolve_item("postgres-locking", _manifest(items))
    assert r.match == "c-1"
    assert r.level == "basename"


def test_resolve_basename_match_with_extension() -> None:
    items = [_snap("c-1", "/foo/postgres-locking.md")]
    r = resolve_item("postgres-locking.md", _manifest(items))
    assert r.match == "c-1"


def test_resolve_basename_case_insensitive() -> None:
    items = [_snap("c-1", "/foo/Postgres-Locking.md")]
    r = resolve_item("postgres-locking", _manifest(items))
    assert r.match == "c-1"


def test_resolve_substring_match_unique() -> None:
    items = [
        _snap("c-1", "/path/postgres-deadlock-fix.md"),
        _snap("c-2", "/path/redis-issue.md"),
    ]
    r = resolve_item("deadlock", _manifest(items))
    assert r.match == "c-1"
    assert r.level == "substring"


def test_resolve_substring_match_ambiguous() -> None:
    items = [
        _snap("c-1", "/foo/postgres-locking.md"),
        _snap("c-2", "/bar/postgres-deadlock.md"),
    ]
    r = resolve_item("postgres", _manifest(items))
    assert r.match is None
    assert sorted(r.candidates) == ["c-1", "c-2"]


def test_resolve_no_match() -> None:
    items = [_snap("c-1", "/foo/x.md")]
    r = resolve_item("does-not-exist", _manifest(items))
    assert r.match is None
    assert r.candidates == []
    assert r.level == "no-match"


def test_exact_id_beats_basename() -> None:
    """If a query happens to match an id exactly AND the basename of another
    item, the exact-id match wins."""
    items = [
        _snap("postgres", "/foo/other.md"),  # weird id; usually c-* prefix
        _snap("c-1", "/foo/postgres.md"),
    ]
    r = resolve_item("postgres", _manifest(items))
    assert r.match == "postgres"


# ---------- resolve_brain_path ----------

@pytest.fixture
def brain(tmp_path: Path) -> Path:
    """A tiny synthetic ~/.agent/-shaped tree."""
    (tmp_path / "semantic" / "lessons").mkdir(parents=True)
    (tmp_path / "personal" / "notes").mkdir(parents=True)
    (tmp_path / "semantic" / "lessons" / "postgres-locking.md").write_text("p")
    (tmp_path / "semantic" / "lessons" / "redis-issue.md").write_text("r")
    (tmp_path / "personal" / "notes" / "today.md").write_text("t")
    # Hidden + __pycache__ should be skipped
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("git")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "junk.pyc").write_text("p")
    return tmp_path


def test_resolve_brain_exact_path(brain: Path) -> None:
    target = brain / "semantic" / "lessons" / "postgres-locking.md"
    r = resolve_brain_path(str(target), brain)
    assert r.match == str(target)
    assert r.level == "exact-path"


def test_resolve_brain_relative_path_to_brain_root(brain: Path) -> None:
    r = resolve_brain_path("semantic/lessons/postgres-locking.md", brain)
    assert r.match == str(brain / "semantic" / "lessons" / "postgres-locking.md")
    assert r.level == "exact-path"


def test_resolve_brain_basename_unique(brain: Path) -> None:
    r = resolve_brain_path("postgres-locking", brain)
    assert "postgres-locking.md" in (r.match or "")
    assert r.level == "basename"


def test_resolve_brain_basename_with_extension(brain: Path) -> None:
    r = resolve_brain_path("postgres-locking.md", brain)
    assert "postgres-locking.md" in (r.match or "")


def test_resolve_brain_substring_unique(brain: Path) -> None:
    r = resolve_brain_path("redis", brain)
    assert "redis-issue.md" in (r.match or "")
    assert r.level == "substring"


def test_resolve_brain_no_match(brain: Path) -> None:
    r = resolve_brain_path("totally-not-here", brain)
    assert r.match is None
    assert r.candidates == []


def test_resolve_brain_skips_hidden_and_pycache(brain: Path) -> None:
    """Should NOT find files under .git/ or __pycache__/."""
    r = resolve_brain_path("config", brain)  # .git/config exists
    assert r.match is None  # we skipped it
    r2 = resolve_brain_path("junk", brain)  # __pycache__/junk.pyc
    assert r2.match is None


def test_resolve_brain_missing_root() -> None:
    r = resolve_brain_path("anything", Path("/does/not/exist/anywhere"))
    assert r.match is None
    assert r.level == "no-brain-root"
