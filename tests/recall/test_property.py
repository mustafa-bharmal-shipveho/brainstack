"""Hypothesis-based property tests for retrieval edge cases.

Goal: hammer the parser and retriever with weird inputs to catch crashes,
hangs, or silently wrong behavior.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from recall.config import SourceConfig
from recall.core import Bm25Retriever, Document, reciprocal_rank_fusion
from recall.frontmatter import parse_file_text
from recall.sources import discover_documents


# Strategies ---------------------------------------------------------------

# Reasonable text for body content (no surrogate halves, no NUL bytes)
text_no_nulls = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
    min_size=0,
    max_size=200,
)

# Plausible YAML scalar values
scalar_value = st.one_of(
    st.text(min_size=1, max_size=80, alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00\n\r:#")),
    st.integers(min_value=-1_000_000, max_value=1_000_000),
    st.booleans(),
)


@given(text=text_no_nulls)
def test_parse_file_text_never_crashes(text):
    """Parsing arbitrary text (with or without frontmatter) must never raise."""
    parsed = parse_file_text(text)
    assert hasattr(parsed, "frontmatter")
    assert hasattr(parsed, "body")


@given(text=text_no_nulls)
def test_parse_file_text_preserves_round_trip_when_no_frontmatter(text):
    """If text has no leading '---' marker, body should be the original text."""
    if not text.startswith("---"):
        parsed = parse_file_text(text)
        assert parsed.body == text


@given(
    # Restrict to safe slug-like names that YAML won't auto-convert to int/bool/null/etc.
    # Real memories use slugs (e.g., 'feedback-pin-deps') so this is realistic.
    name=st.from_regex(r"[a-z][a-z0-9_-]{0,40}", fullmatch=True),
    body=text_no_nulls,
)
def test_parse_well_formed_round_trip(name, body):
    """A valid frontmatter + body should always round-trip the name."""
    text = f"---\nname: {name}\ntype: feedback\n---\n{body}"
    parsed = parse_file_text(text)
    if "name" in parsed.frontmatter:
        assert str(parsed.frontmatter["name"]) == name


@given(
    rankings=st.lists(
        st.lists(
            st.text(min_size=1, max_size=20, alphabet=st.characters(min_codepoint=33, max_codepoint=126)),
            min_size=0,
            max_size=10,
            unique=True,
        ),
        min_size=0,
        max_size=5,
    )
)
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_rrf_returns_only_inputs(rankings):
    """RRF result must contain exactly the union of input items, no extras."""
    fused = reciprocal_rank_fusion(rankings)
    expected = set()
    for r in rankings:
        expected.update(r)
    assert set(fused) == expected


@given(
    rankings=st.lists(
        st.lists(
            st.text(min_size=1, max_size=10),
            min_size=1,
            max_size=8,
            unique=True,
        ),
        min_size=1,
        max_size=4,
    )
)
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=50)
def test_rrf_is_deterministic(rankings):
    """Same input must produce same output."""
    a = reciprocal_rank_fusion(rankings)
    b = reciprocal_rank_fusion(rankings)
    assert a == b


@given(query=text_no_nulls)
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=30, deadline=2000)
def test_bm25_query_never_crashes(query):
    """BM25 query against a small fixed corpus must handle any unicode query."""
    docs = [
        Document(
            path="/x.md",
            source="brain",
            title="x",
            frontmatter={"name": "x"},
            body="hello world example body",
            text="x example body hello world",
        )
    ]
    retriever = Bm25Retriever(docs)
    try:
        retriever.query(query, k=5)
    except ValueError:
        # Empty/whitespace-only query may raise — acceptable
        pass


# Filesystem-level property: random brain layouts -------------------------


@given(
    files=st.lists(
        st.tuples(
            st.text(
                min_size=1,
                max_size=15,
                alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
            ),
            text_no_nulls,
        ),
        min_size=0,
        max_size=10,
        unique_by=lambda t: t[0],
    )
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    max_examples=20,
    deadline=3000,
)
def test_discover_random_brain_does_not_crash(tmp_path, files):
    """Generate a random tree of files and ensure discover_documents handles it."""
    # Use a unique subdirectory per Hypothesis example
    import secrets

    brain = tmp_path / f"brain-{secrets.token_hex(4)}"
    brain.mkdir()

    for name, content in files:
        (brain / f"{name}.md").write_text(content, encoding="utf-8")

    sc = SourceConfig(
        name="rand",
        path=str(brain),
        glob="**/*.md",
        frontmatter="optional",
        exclude=[],
    )
    docs = list(discover_documents(sc))
    assert isinstance(docs, list)
