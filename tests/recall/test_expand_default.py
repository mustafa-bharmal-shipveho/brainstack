"""Red-phase tests: query expansion becomes opt-in (default OFF).

Planned contract (not implemented yet):

  - `RankingConfig` gains `expand_default: bool = False`.
  - `recall query --expand/--no-expand` becomes tri-state: when neither
    flag is passed (None), the CLI resolves to `cfg.ranking.expand_default`.
  - Net effect: a bare `recall query ...` must NOT call
    `recall.expand.expand_query` (today it does: the flag defaults to True,
    which costs an LLM round-trip per query for users who never asked).
  - `--expand` still engages expansion; `ranking.expand_default: true` in
    config engages it without the flag.

Hermetic: fastembed factories stubbed per the test_incremental_index.py
pattern; expand_query is always monkeypatched so no LLM provider is ever
resolved.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from recall import qdrant_backend
from recall.cli import app

_DENSE_DIM = qdrant_backend._DENSE_DIM


@pytest.fixture(autouse=True)
def _reset_qdrant_caches(isolated_xdg, monkeypatch):
    monkeypatch.delenv("RECALL_MODE", raising=False)
    qdrant_backend._reset_client_cache_for_tests()
    qdrant_backend._reset_model_cache_for_tests()
    yield
    qdrant_backend._reset_client_cache_for_tests()
    qdrant_backend._reset_model_cache_for_tests()


@pytest.fixture
def stub_embedders(monkeypatch):
    """Deterministic fastembed stand-ins; no model downloads, no GPU."""

    class _DenseStub:
        def embed(self, texts):
            return [[0.1] * _DENSE_DIM for _ in list(texts)]

        def query_embed(self, texts):
            return [[0.1] * _DENSE_DIM for _ in list(texts)]

    class _SparseStub:
        def embed(self, texts):
            for _ in list(texts):
                v = MagicMock()
                v.indices = [0]
                v.values = [0.1]
                yield v

        def query_embed(self, texts):
            for _ in list(texts):
                v = MagicMock()
                v.indices = [0]
                v.values = [0.1]
                yield v

    class _CrossStub:
        def rerank(self, query, texts):
            return [1.0 / (i + 1) for i in range(len(list(texts)))]

    monkeypatch.setattr(qdrant_backend, "_get_embedder", lambda *a, **kw: _DenseStub())
    monkeypatch.setattr(
        qdrant_backend, "_get_sparse_embedder", lambda *a, **kw: _SparseStub()
    )
    monkeypatch.setattr(
        qdrant_backend, "_get_cross_encoder", lambda *a, **kw: _CrossStub()
    )
    return monkeypatch


def _brain_source(brain: Path) -> dict:
    return {
        "name": "brain",
        "path": str(brain),
        "glob": "**/*.md",
        "frontmatter": "auto-memory",
        "exclude": [],
    }


def _setup_indexed_brain(
    write_config, auto_memory_brain, *, ranking_extra: dict | None = None
) -> CliRunner:
    # reranker pinned to "none": _config_from_dict's absent-key default is
    # "cross_encoder", which would pull a real cross-encoder model.
    ranking = {"mode": "hybrid", "reranker": "none"}
    if ranking_extra:
        ranking.update(ranking_extra)
    write_config(
        sources=[_brain_source(auto_memory_brain)],
        extra={"ranking": ranking},
    )
    runner = CliRunner()
    result = runner.invoke(app, ["reindex"])
    assert result.exit_code == 0, f"reindex failed during setup: {result.output}"
    return runner


class TestExpandDefaultOff:
    def test_query_default_skips_expansion(
        self, stub_embedders, monkeypatch, isolated_xdg, write_config, auto_memory_brain
    ):
        """A bare `recall query` must not pay the expansion LLM round-trip."""
        runner = _setup_indexed_brain(write_config, auto_memory_brain)

        def _boom(*args, **kwargs):
            raise AssertionError(
                "expand_query must not be called on the default query path"
            )

        monkeypatch.setattr("recall.expand.expand_query", _boom)

        result = runner.invoke(app, ["query", "atomic", "writes"])
        assert result.exit_code == 0, (
            f"default query must succeed without expansion; "
            f"got exit {result.exit_code}:\n{result.output}"
        )
        data = json.loads(result.stdout)
        assert isinstance(data, list) and data, "query should still return results"

    def test_ranking_config_has_expand_default_false(self):
        from recall.config import RankingConfig

        cfg = RankingConfig()
        assert cfg.expand_default is False, (
            "RankingConfig.expand_default must exist and default to False"
        )


class TestExpandOptIn:
    def test_expand_flag_engages_expansion(
        self, stub_embedders, monkeypatch, isolated_xdg, write_config, auto_memory_brain
    ):
        runner = _setup_indexed_brain(write_config, auto_memory_brain)

        calls: list[tuple[str, int]] = []

        def _fake_expand(query, n=3, provider=None):
            calls.append((query, n))
            return [query, f"paraphrase of {query}"]

        monkeypatch.setattr("recall.expand.expand_query", _fake_expand)

        result = runner.invoke(app, ["query", "--expand", "atomic", "writes"])
        assert result.exit_code == 0, result.output
        assert calls, "--expand must engage expand_query"
        assert calls[0][0] == "atomic writes"

    def test_config_expand_default_true_engages(
        self, stub_embedders, monkeypatch, isolated_xdg, write_config, auto_memory_brain
    ):
        """No flag passed: ranking.expand_default=true turns expansion on."""
        runner = _setup_indexed_brain(
            write_config, auto_memory_brain, ranking_extra={"expand_default": True}
        )

        calls: list[tuple[str, int]] = []

        def _fake_expand(query, n=3, provider=None):
            calls.append((query, n))
            return [query, f"paraphrase of {query}"]

        monkeypatch.setattr("recall.expand.expand_query", _fake_expand)

        result = runner.invoke(app, ["query", "atomic", "writes"])
        assert result.exit_code == 0, result.output
        assert calls, (
            "with ranking.expand_default=true and no flag, expansion must engage"
        )
