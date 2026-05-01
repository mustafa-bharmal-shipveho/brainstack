"""Synthetic path-normalization test.

The manifest's source_path field is stored verbatim. Tests verify the
manifest preserves whatever path the runtime gave it without normalizing
or rewriting (preserves forensic value), and that paths with unusual but
valid characters round-trip cleanly.

The RUNTIME upstream of the manifest is responsible for picking a
normalization scheme (e.g., always relative to ~/.agent/, always POSIX
separators). Tests for that live in the integration suite (sub-phase 3g).
"""
from __future__ import annotations

import pytest

from runtime.core.manifest import (
    SCHEMA_VERSION,
    InjectionItemSnapshot,
    Manifest,
    dump_manifest,
    load_manifest,
)


@pytest.mark.parametrize("path", [
    "memory/lessons/foo.md",
    "memory/lessons/résumé.md",
    "memory/lessons/with space.md",
    "memory/lessons/with-dash.md",
    "memory/lessons/with_underscore.md",
    "memory/lessons/with.dots.md",
    "/abs/path/to/file.md",
    "relative/file.md",
    "memory/日本語/lessons.md",
    "memory/lessons/quote'name.md",
    "memory/lessons/symbols!@#.md",
])
def test_path_round_trips_byte_identical(path: str) -> None:
    m = Manifest(
        schema_version=SCHEMA_VERSION,
        turn=1,
        ts_ms=1,
        session_id="x",
        budget_total=100,
        budget_used=10,
        items=[
            InjectionItemSnapshot(
                id="c-1",
                bucket="hot",
                source_path=path,
                sha256="0" * 64,
                token_count=10,
                retrieval_reason="r",
                last_touched_turn=1,
                pinned=False,
            ),
        ],
    )
    a = dump_manifest(m)
    b = dump_manifest(load_manifest(a))
    assert a == b
    assert load_manifest(a).items[0].source_path == path


def test_paths_with_different_separators_are_kept_literal() -> None:
    """`a/b/c.md` and `a\\b\\c.md` are different paths; the manifest does
    not normalize. The producing layer chooses the convention."""
    m_unix = _single_path_manifest("a/b/c.md")
    m_win = _single_path_manifest("a\\b\\c.md")
    assert dump_manifest(m_unix) != dump_manifest(m_win)


def _single_path_manifest(path: str) -> Manifest:
    return Manifest(
        schema_version=SCHEMA_VERSION,
        turn=1,
        ts_ms=1,
        session_id="x",
        budget_total=100,
        budget_used=10,
        items=[
            InjectionItemSnapshot(
                id="c-1",
                bucket="hot",
                source_path=path,
                sha256="0" * 64,
                token_count=10,
                retrieval_reason="r",
                last_touched_turn=1,
                pinned=False,
            ),
        ],
    )


def test_trailing_slash_preserved() -> None:
    a = _single_path_manifest("dir/")
    b = _single_path_manifest("dir")
    assert dump_manifest(a) != dump_manifest(b)
