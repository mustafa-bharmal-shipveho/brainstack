"""Red-phase tests: sparse-only retrieval mode + dense-failure auto-fallback.

Planned contract (not implemented yet):

  - `HybridRetriever(..., mode='hybrid'|'dense'|'sparse')`. On the query
    path, sparse mode NEVER constructs the dense embedder (so a brain
    indexed before the bge model download still answers queries).
  - In hybrid mode, when the dense embedder factory raises, the query
    auto-falls back to the sparse leg and prints EXACTLY ONE stderr
    warning per process. The warning mentions 'recall reindex' and
    'RECALL_MODE' so the user knows both the fix and the override.
  - CLI: `recall query --mode sparse|dense|hybrid`. Precedence:
    flag > RECALL_MODE env var > config ranking.mode.

Hermetic per the established pattern (test_incremental_index.py): fastembed
factories are monkeypatched with deterministic stubs; the embedded Qdrant
store is real but lives under isolated_xdg.

Test order inside this file matters for the once-per-process warning: the
exactly-once test runs first so no earlier fallback consumes the warning.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from recall import qdrant_backend
from recall.cli import app
from recall.core import Document, HybridRetriever

_DENSE_DIM = qdrant_backend._DENSE_DIM
SOURCE = "brain"


@pytest.fixture(autouse=True)
def _reset_qdrant_caches(isolated_xdg, monkeypatch):
    """Fresh client + model caches per test; no RECALL_MODE leaking in."""
    monkeypatch.delenv("RECALL_MODE", raising=False)
    qdrant_backend._reset_client_cache_for_tests()
    qdrant_backend._reset_model_cache_for_tests()
    yield
    qdrant_backend._reset_client_cache_for_tests()
    qdrant_backend._reset_model_cache_for_tests()


@pytest.fixture
def stub_embedders(monkeypatch):
    """Deterministic fastembed stand-ins (same shape as test_incremental_index)."""

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


def _poison_dense(monkeypatch, exc_type: type, msg: str) -> None:
    """Make any dense-embedder construction blow up with `exc_type`."""

    def _boom(*args, **kwargs):
        raise exc_type(msg)

    monkeypatch.setattr(qdrant_backend, "_get_embedder", _boom)


def _make_doc(name: str, description: str) -> Document:
    return Document(
        path=f"/synth/{SOURCE}/{name}.md",
        source=SOURCE,
        title=name,
        frontmatter={"name": name, "description": description, "type": "reference"},
        body=description,
        text=f"{name} {description}",
    )


def _index_small_corpus() -> list[Document]:
    """Upsert 3 docs (both legs) so the collection has data to fall back on."""
    docs = [
        _make_doc("python-gil", "global interpreter lock prevents parallel cpu work"),
        _make_doc("rust-borrow", "ownership and lifetimes prevent memory bugs"),
        _make_doc("go-channels", "channels coordinate goroutines"),
    ]
    HybridRetriever(docs)
    return docs


def _reset_fallback_warning_state() -> None:
    """Best-effort reset of the once-per-process warning flag.

    The flag does not exist yet (red phase). Once implemented, a test hook
    named like the candidates below keeps this file order-independent; until
    then this is a harmless no-op.
    """
    from recall import core as core_mod

    for mod in (qdrant_backend, core_mod):
        for name in (
            "_reset_sparse_fallback_warning_for_tests",
            "_reset_fallback_warning_for_tests",
            "_reset_dense_fallback_warning_for_tests",
        ):
            fn = getattr(mod, name, None)
            if callable(fn):
                fn()


class TestHybridDenseFailureFallback:
    def test_fallback_warning_printed_exactly_once_per_process(
        self, stub_embedders, monkeypatch, capsys
    ):
        """Two queries (and a second retriever) produce ONE stderr warning."""
        _index_small_corpus()
        _poison_dense(monkeypatch, RuntimeError, "simulated dense model load failure")
        _reset_fallback_warning_state()
        capsys.readouterr()  # drain anything buffered before the measurement

        retriever = HybridRetriever(documents=None, collections=[SOURCE])
        r1 = retriever.query("global interpreter lock", k=3)
        r2 = retriever.query("ownership lifetimes", k=3)
        retriever2 = HybridRetriever(documents=None, collections=[SOURCE])
        r3 = retriever2.query("goroutine channels", k=3)

        err = capsys.readouterr().err
        assert err.count("recall reindex") == 1, (
            f"expected exactly one fallback warning mentioning 'recall reindex', "
            f"stderr was:\n{err}"
        )
        assert err.count("RECALL_MODE") == 1, (
            f"the single warning must mention RECALL_MODE, stderr was:\n{err}"
        )
        assert r1 and r2 and r3, "all fallback queries must still return results"

    def test_query_falls_back_to_sparse_when_dense_embedder_raises(
        self, stub_embedders, monkeypatch
    ):
        _index_small_corpus()
        _poison_dense(monkeypatch, RuntimeError, "simulated dense model load failure")

        retriever = HybridRetriever(documents=None, collections=[SOURCE])
        results = retriever.query("global interpreter lock", k=3)
        assert len(results) >= 1, (
            "hybrid query must degrade to the sparse leg instead of raising"
        )
        assert all(r.document.source == SOURCE for r in results)


class TestSparseMode:
    def test_mode_sparse_never_constructs_dense_embedder(
        self, stub_embedders, monkeypatch
    ):
        _index_small_corpus()
        # AssertionError (not RuntimeError) so any accidental construction
        # fails the test rather than tripping the hybrid fallback path.
        _poison_dense(
            monkeypatch,
            AssertionError,
            "dense embedder must not be constructed in sparse mode",
        )

        retriever = HybridRetriever(
            documents=None, collections=[SOURCE], mode="sparse"
        )
        results = retriever.query("global interpreter lock", k=3)
        assert len(results) >= 1, "sparse-mode query must work without the dense model"


def _brain_source(brain: Path) -> dict:
    return {
        "name": SOURCE,
        "path": str(brain),
        "glob": "**/*.md",
        "frontmatter": "auto-memory",
        "exclude": [],
    }


class TestCliModeSelection:
    """`recall query --mode` + RECALL_MODE env precedence (flag > env > config)."""

    def _setup_indexed_brain(self, write_config, auto_memory_brain) -> CliRunner:
        # reranker pinned to "none": the absent-ranking default in
        # _config_from_dict is "cross_encoder", which would drag in a real
        # cross-encoder model.
        write_config(
            sources=[_brain_source(auto_memory_brain)],
            extra={"ranking": {"mode": "hybrid", "reranker": "none"}},
        )
        runner = CliRunner()
        result = runner.invoke(app, ["reindex"])
        assert result.exit_code == 0, f"reindex failed during setup: {result.output}"
        return runner

    def test_cli_mode_flag_overrides_env(
        self, stub_embedders, monkeypatch, isolated_xdg, write_config, auto_memory_brain
    ):
        runner = self._setup_indexed_brain(write_config, auto_memory_brain)
        monkeypatch.setenv("RECALL_MODE", "dense")
        _poison_dense(
            monkeypatch,
            AssertionError,
            "--mode sparse must win over RECALL_MODE=dense",
        )

        result = runner.invoke(
            app, ["query", "--mode", "sparse", "--no-expand", "atomic", "writes"]
        )
        assert result.exit_code == 0, (
            f"--mode sparse should beat RECALL_MODE=dense and succeed without the "
            f"dense embedder; got exit {result.exit_code}:\n{result.output}"
        )
        data = json.loads(result.stdout)
        assert isinstance(data, list) and data, "sparse query should return results"

    def test_recall_mode_env_forces_sparse(
        self, stub_embedders, monkeypatch, isolated_xdg, write_config, auto_memory_brain
    ):
        runner = self._setup_indexed_brain(write_config, auto_memory_brain)
        monkeypatch.setenv("RECALL_MODE", "sparse")
        _poison_dense(
            monkeypatch,
            AssertionError,
            "RECALL_MODE=sparse must prevent dense embedder construction",
        )

        result = runner.invoke(app, ["query", "--no-expand", "atomic", "writes"])
        assert result.exit_code == 0, (
            f"RECALL_MODE=sparse should override the config's hybrid mode; "
            f"got exit {result.exit_code}:\n{result.output}"
        )
        data = json.loads(result.stdout)
        assert isinstance(data, list) and data, "sparse query should return results"
