"""Gap U: source-level auto-recall scoping.

The adoption audit flagged that auto-recall queries every configured source
on every prompt, so a sensitive mirrored source (e.g. an `--add-source`
folder of employer-internal notes) gets injected into sessions on unrelated
repositories. The fix is a per-config `auto_recall.exclude_sources` list: named
sources are excluded from the every-prompt injection while staying fully
available to explicit `recall query` and the MCP surface.

These tests are hermetic: they exercise the pure collection-selection helper
and config round-trip, with no Qdrant or embedder.
"""
from __future__ import annotations

import json


def _cfg(exclude):
    from recall.config import AutoRecallConfig, Config, SourceConfig

    return Config(
        sources=[
            SourceConfig(
                name="lessons",
                path="$BRAIN_ROOT/memory/semantic/lessons",
                glob="**/*.md",
                frontmatter="optional",
            ),
            SourceConfig(
                name="imports",
                path="$BRAIN_ROOT/imports",
                glob="**/*.md",
                frontmatter="optional",
            ),
        ],
        auto_recall=AutoRecallConfig(exclude_sources=exclude),
    )


def test_default_auto_recall_config_excludes_nothing():
    from recall.config import AutoRecallConfig

    assert AutoRecallConfig().exclude_sources == []


def test_no_exclusion_includes_all_sources():
    from runtime.adapters.claude_code.auto_recall import _auto_recall_collections

    assert sorted(_auto_recall_collections(_cfg([]))) == ["imports", "lessons"]


def test_excluded_source_dropped_from_auto_recall():
    from runtime.adapters.claude_code.auto_recall import _auto_recall_collections

    cols = _auto_recall_collections(_cfg(["imports"]))
    assert "imports" not in cols
    assert "lessons" in cols


def test_exclude_unknown_source_is_noop():
    from runtime.adapters.claude_code.auto_recall import _auto_recall_collections

    assert sorted(_auto_recall_collections(_cfg(["nonexistent"]))) == [
        "imports",
        "lessons",
    ]


def test_exclude_all_sources_yields_empty_then_retriever_returns_nothing():
    # Excluding every source is the user's explicit choice to disable
    # auto-recall via config; the collection list is empty (the retriever
    # then returns no results rather than erroring).
    from runtime.adapters.claude_code.auto_recall import _auto_recall_collections

    assert _auto_recall_collections(_cfg(["lessons", "imports"])) == []


def test_config_round_trip_preserves_exclude_sources(tmp_path, monkeypatch):
    from recall import config as cfgmod

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    cfg = _cfg(["imports"])
    d = cfgmod._config_to_dict(cfg)
    assert d["auto_recall"]["exclude_sources"] == ["imports"]
    # Reparse from the serialized dict (must round-trip through JSON cleanly).
    reparsed = cfgmod._config_from_dict(json.loads(json.dumps(d)))
    assert reparsed.auto_recall.exclude_sources == ["imports"]


def test_legacy_config_without_auto_recall_key_parses_with_empty_default():
    # A config written before this feature has no `auto_recall` key. It must
    # parse cleanly with the default (exclude nothing), preserving behavior.
    from recall.config import _config_from_dict

    legacy = {
        "sources": [
            {"name": "lessons", "path": "$BRAIN_ROOT/memory", "glob": "**/*.md",
             "frontmatter": "optional"}
        ],
        "ranking": {},
        "default_k": 5,
    }
    cfg = _config_from_dict(legacy)
    assert cfg.auto_recall.exclude_sources == []
