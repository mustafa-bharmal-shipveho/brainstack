"""SDK surface tests for v0.2-rc1.

Covers the public API in agent/memory/sdk.py:
  - append_episodic stamps schema_version + ts and writes correctly
  - default namespace preserves v0.1 path layout
  - non-default namespaces nest under <root>/memory/<area>/<ns>/
  - query_semantic last-k vs substring filter
  - read_policy / write_policy round-trip and missing-file behavior
  - invalid namespace names raise ValueError
"""
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SDK_DIR = REPO_ROOT / "agent" / "memory"
sys.path.insert(0, str(SDK_DIR))
sys.path.insert(0, str(REPO_ROOT / "agent" / "harness" / "hooks"))

import sdk  # noqa: E402


@pytest.fixture
def brain(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path))
    return tmp_path


def _read_lines(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(l) for l in f.read().splitlines() if l.strip()]


def test_append_episodic_default_namespace_uses_v01_path(brain):
    """Default ns must keep AGENT_LEARNINGS.jsonl at memory/episodic/ (no subdir)."""
    out = sdk.append_episodic("default", {"id": "e1", "summary": "hello"})
    expected_path = brain / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl"
    assert expected_path.exists()
    rows = _read_lines(expected_path)
    assert len(rows) == 1
    assert rows[0]["id"] == "e1"
    assert rows[0]["schema_version"] == 1
    assert "ts" in rows[0]
    # Returned event mirrors the file contents.
    assert out["id"] == "e1"


def test_append_episodic_custom_namespace_nests(brain):
    """A custom ns must write under memory/episodic/<ns>/."""
    sdk.append_episodic("inbox", {"id": "i1", "summary": "in1"})
    expected = brain / "memory" / "episodic" / "inbox" / "AGENT_LEARNINGS.jsonl"
    assert expected.exists()
    rows = _read_lines(expected)
    assert rows[0]["id"] == "i1"
    # Default-ns file should NOT exist (cross-contamination check).
    assert not (brain / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl").exists()


def test_append_episodic_preserves_caller_provided_fields(brain):
    custom_ts = "2026-01-02T03:04:05+00:00"
    out = sdk.append_episodic("default", {
        "id": "e1", "summary": "x", "schema_version": 7, "ts": custom_ts,
    })
    assert out["schema_version"] == 7
    assert out["ts"] == custom_ts


def test_append_episodic_returns_stamped_event(brain):
    ev = sdk.append_episodic("default", {"id": "e2", "summary": "y"})
    assert ev["schema_version"] == 1
    assert "ts" in ev


# --- PR1: origin stamping --------------------------------------------------

def test_append_episodic_stamps_default_origin_when_missing(brain):
    """Backward-compat: events without `origin` get `coding.tool_call`.

    The canonical writer (claude_code_post_tool.py) stamps origin
    explicitly going forward, but third-party callers that worked
    before PR1 must keep working.
    """
    ev = sdk.append_episodic("default", {"id": "e3", "summary": "no origin"})
    assert ev["origin"] == "coding.tool_call"


def test_append_episodic_preserves_explicit_origin(brain):
    """Caller-provided origin is not overwritten."""
    ev = sdk.append_episodic("inbox", {
        "id": "e4",
        "summary": "explicit",
        "origin": "agentry.inbox.action",
    })
    assert ev["origin"] == "agentry.inbox.action"


def _seed_lessons(brain, namespace, rows):
    if namespace == "default":
        sem = brain / "memory" / "semantic"
    else:
        sem = brain / "memory" / "semantic" / namespace
    sem.mkdir(parents=True, exist_ok=True)
    path = sem / "lessons.jsonl"
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


def test_query_semantic_last_k_when_no_query(brain):
    rows = [{"claim": f"c{i}", "why": "", "how_to_apply": ""} for i in range(15)]
    _seed_lessons(brain, "default", rows)
    out = sdk.query_semantic("default", k=5)
    assert len(out) == 5
    assert [r["claim"] for r in out] == ["c10", "c11", "c12", "c13", "c14"]


def test_query_semantic_substring_filters_claim_why_how_to_apply(brain):
    rows = [
        {"claim": "use pnpm not npm", "why": "", "how_to_apply": ""},
        {"claim": "ALWAYS use BRAIN_ROOT env", "why": "secure path",
         "how_to_apply": ""},
        {"claim": "unrelated", "why": "", "how_to_apply": "run pnpm install"},
    ]
    _seed_lessons(brain, "default", rows)
    # claim hit
    out = sdk.query_semantic("default", query="pnpm", k=10)
    claims = {r["claim"] for r in out}
    assert "use pnpm not npm" in claims
    assert "unrelated" in claims
    # why hit
    out2 = sdk.query_semantic("default", query="secure", k=10)
    assert len(out2) == 1
    # case-insensitive
    out3 = sdk.query_semantic("default", query="ALWAYS", k=10)
    assert len(out3) == 1


def test_query_semantic_namespace_isolated(brain):
    _seed_lessons(brain, "default", [{"claim": "default-only"}])
    _seed_lessons(brain, "inbox", [{"claim": "inbox-only"}])
    out = sdk.query_semantic("inbox", k=10)
    assert len(out) == 1
    assert out[0]["claim"] == "inbox-only"


def test_query_semantic_missing_file_returns_empty(brain):
    assert sdk.query_semantic("default", k=5) == []
    assert sdk.query_semantic("inbox", query="x", k=5) == []


def test_policy_round_trip_default_namespace(brain):
    pol = {"tiers": {"sender@x.com": {"tier": 2, "since": "now"}}}
    sdk.write_policy("default", pol)
    got = sdk.read_policy("default")
    assert got == pol


def test_policy_round_trip_custom_namespace(brain):
    pol = {"tiers": {"alice": {"tier": 2}}, "rules": ["x", "y"]}
    sdk.write_policy("inbox", pol)
    got = sdk.read_policy("inbox")
    assert got == pol
    # Default-ns policy should be untouched.
    assert sdk.read_policy("default") == {}


def test_read_policy_missing_file_returns_empty_dict(brain):
    assert sdk.read_policy("default") == {}
    assert sdk.read_policy("inbox") == {}


def test_invalid_namespace_raises_value_error(brain):
    bad_names = [
        "Bad",          # uppercase
        "1abc",         # leading digit
        "with space",   # space
        "with/slash",   # slash
        "",             # empty
        "a" * 33,       # too long (max 32)
    ]
    for bad in bad_names:
        with pytest.raises(ValueError):
            sdk.append_episodic(bad, {"id": "x"})
        with pytest.raises(ValueError):
            sdk.query_semantic(bad)
        with pytest.raises(ValueError):
            sdk.read_policy(bad)
        with pytest.raises(ValueError):
            sdk.write_policy(bad, {})


def test_default_namespace_accepted(brain):
    """`default` is the v0.1 backward-compat sentinel and must always be valid."""
    sdk.append_episodic("default", {"id": "ok"})


def test_brain_root_arg_overrides_env(tmp_path, monkeypatch, brain):
    """Explicit brain_root kwarg wins over BRAIN_ROOT env."""
    other = tmp_path / "other-brain"
    sdk.append_episodic("default", {"id": "x"}, brain_root=str(other))
    assert (other / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl").exists()
    # The env-pointed brain should NOT have received the write.
    assert not (brain / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl").exists()
