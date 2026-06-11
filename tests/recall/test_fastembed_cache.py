"""Red-phase tests: FastEmbed model-cache directory resolution.

Planned contract (recall/qdrant_backend.py, not implemented yet):

  - `_fastembed_cache_dir() -> str`
      * honors FASTEMBED_CACHE_PATH env var first
      * else resolves to XDG_CACHE_HOME/fastembed (default ~/.cache/fastembed)
      * creates the directory (mkdir, parents ok)
  - all three embedder factories (`_get_embedder`, `_get_sparse_embedder`,
    `_get_cross_encoder`) pass `cache_dir=_fastembed_cache_dir()` to their
    FastEmbed constructors, so model weights land in a predictable,
    doctor-reportable location instead of wherever fastembed defaults to.

Follows the established stubbing pattern (see test_incremental_index.py):
never download model weights; capture constructor kwargs instead. The stubs
replace the fastembed classes themselves because the factories import them
lazily inside the function body (qdrant_backend.py:50-51 comment).

`_fastembed_cache_dir` does not exist yet, so it is imported lazily inside
test bodies: collection succeeds, the test fails red with an ImportError.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _reset_caches(isolated_xdg):
    """Hermetic per-test state: tmp XDG dirs + fresh client/model caches."""
    from recall import qdrant_backend as qb

    qb._reset_client_cache_for_tests()
    qb._reset_model_cache_for_tests()
    yield
    qb._reset_client_cache_for_tests()
    qb._reset_model_cache_for_tests()


@pytest.fixture
def captured_kwargs(monkeypatch):
    """Replace the three FastEmbed classes with kwarg-capturing stubs.

    Returns {"dense": [...], "sparse": [...], "cross": [...]} where each
    entry is the kwargs dict a constructor call received.
    """
    captured: dict[str, list[dict]] = {"dense": [], "sparse": [], "cross": []}

    def _make_stub(bucket: str):
        class _CapturingStub:
            def __init__(self, model_name=None, **kwargs):
                entry = dict(kwargs)
                entry["model_name"] = model_name
                captured[bucket].append(entry)

        return _CapturingStub

    import fastembed
    import fastembed.rerank.cross_encoder as ce_mod

    monkeypatch.setattr(fastembed, "TextEmbedding", _make_stub("dense"))
    monkeypatch.setattr(fastembed, "SparseTextEmbedding", _make_stub("sparse"))
    monkeypatch.setattr(ce_mod, "TextCrossEncoder", _make_stub("cross"))
    return captured


def _expected_xdg_fastembed_dir() -> str:
    return str(Path(os.environ["XDG_CACHE_HOME"]) / "fastembed")


class TestFactoriesPassCacheDir:
    """Each factory must pin cache_dir so weights land in ONE known place."""

    def test_dense_factory_passes_xdg_cache_dir(self, captured_kwargs, monkeypatch):
        monkeypatch.delenv("FASTEMBED_CACHE_PATH", raising=False)
        from recall import qdrant_backend as qb

        qb._get_embedder("stub/dense-cache-check")
        assert captured_kwargs["dense"], "dense factory should construct the embedder"
        kwargs = captured_kwargs["dense"][0]
        assert kwargs.get("cache_dir") == _expected_xdg_fastembed_dir(), (
            f"dense factory must pass cache_dir=XDG_CACHE_HOME/fastembed, "
            f"got kwargs {kwargs!r}"
        )

    def test_sparse_factory_passes_xdg_cache_dir(self, captured_kwargs, monkeypatch):
        monkeypatch.delenv("FASTEMBED_CACHE_PATH", raising=False)
        from recall import qdrant_backend as qb

        qb._get_sparse_embedder("stub/sparse-cache-check")
        assert captured_kwargs["sparse"], "sparse factory should construct the embedder"
        kwargs = captured_kwargs["sparse"][0]
        assert kwargs.get("cache_dir") == _expected_xdg_fastembed_dir(), (
            f"sparse factory must pass cache_dir=XDG_CACHE_HOME/fastembed, "
            f"got kwargs {kwargs!r}"
        )

    def test_cross_encoder_factory_passes_xdg_cache_dir(self, captured_kwargs, monkeypatch):
        monkeypatch.delenv("FASTEMBED_CACHE_PATH", raising=False)
        from recall import qdrant_backend as qb

        qb._get_cross_encoder("stub/cross-cache-check")
        assert captured_kwargs["cross"], "cross-encoder factory should construct the model"
        kwargs = captured_kwargs["cross"][0]
        assert kwargs.get("cache_dir") == _expected_xdg_fastembed_dir(), (
            f"cross-encoder factory must pass cache_dir=XDG_CACHE_HOME/fastembed, "
            f"got kwargs {kwargs!r}"
        )

    def test_env_override_wins_in_factory(self, captured_kwargs, monkeypatch, tmp_path):
        override = tmp_path / "custom-fastembed-weights"
        monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(override))
        from recall import qdrant_backend as qb

        qb._get_embedder("stub/dense-env-override")
        kwargs = captured_kwargs["dense"][0]
        assert kwargs.get("cache_dir") == str(override), (
            f"FASTEMBED_CACHE_PATH must win over the XDG default, got {kwargs!r}"
        )


class TestFastembedCacheDirHelper:
    """`_fastembed_cache_dir` is the importable doc-truth for where weights live."""

    def test_helper_resolves_to_xdg_cache_home(self, monkeypatch):
        monkeypatch.delenv("FASTEMBED_CACHE_PATH", raising=False)
        # Lazy import: symbol does not exist yet, so this fails red here
        # instead of breaking collection.
        from recall.qdrant_backend import _fastembed_cache_dir

        assert _fastembed_cache_dir() == _expected_xdg_fastembed_dir()

    def test_helper_env_override_wins(self, monkeypatch, tmp_path):
        override = tmp_path / "elsewhere" / "fastembed-cache"
        monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(override))
        from recall.qdrant_backend import _fastembed_cache_dir

        assert _fastembed_cache_dir() == str(override)

    def test_helper_creates_the_directory(self, monkeypatch):
        monkeypatch.delenv("FASTEMBED_CACHE_PATH", raising=False)
        from recall.qdrant_backend import _fastembed_cache_dir

        out = Path(_fastembed_cache_dir())
        assert out.is_dir(), f"_fastembed_cache_dir() must mkdir its result: {out}"

    def test_helper_creates_env_override_directory(self, monkeypatch, tmp_path):
        override = tmp_path / "nested" / "override-cache"
        assert not override.exists()
        monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(override))
        from recall.qdrant_backend import _fastembed_cache_dir

        out = Path(_fastembed_cache_dir())
        assert out == override
        assert out.is_dir(), "the env-override path must be created too"
