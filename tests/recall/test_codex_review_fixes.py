"""Regression tests for the four Codex-review findings on the audit branch.

1. CLI `--mode sparse` must not trigger a dense build (mode threaded into
   `_load_or_build` -> `build_index` -> `upsert_documents`).
2. MCP `recall_query` must honor the effective retrieval mode on both index
   build and query.
3. `recall doctor`'s secret-scanner check must find the git repo at the brain
   root even when the resolved brain home is the `memory/` subdir.
4. (systemd brain-root expansion is covered in tests/test_systemd_setup.py.)

All hermetic: no Qdrant server, no embedder, no network.
"""
from __future__ import annotations

from pathlib import Path

import pytest


# --- 1. CLI mode propagation into the index build -------------------------

def test_load_or_build_threads_mode_into_build_index(monkeypatch):
    from recall import cli

    captured = {}

    def fake_needs_refresh(sources):
        return True

    def fake_build_index(sources, mode="hybrid"):
        captured["mode"] = mode
        return object()  # truthy cache stand-in; query path not exercised here

    monkeypatch.setattr(cli, "needs_refresh", fake_needs_refresh)
    monkeypatch.setattr(cli, "build_index", fake_build_index)

    class _Cfg:
        sources = []

    cli._load_or_build(_Cfg(), mode="sparse")
    assert captured["mode"] == "sparse"


def test_build_index_passes_mode_to_upsert(monkeypatch, tmp_path):
    from recall import index as index_mod

    calls = []

    class _FakeClient:
        pass

    monkeypatch.setattr(index_mod.qb, "_qdrant_client_singleton", lambda base: _FakeClient())
    monkeypatch.setattr(index_mod, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(index_mod, "_legacy_cache_cleanup", lambda base: None)
    monkeypatch.setattr(index_mod.qb, "ensure_collection", lambda c, n: None)
    monkeypatch.setattr(index_mod.qb, "delete_points_not_in_paths", lambda c, n, p: None)
    monkeypatch.setattr(index_mod, "discover_documents", lambda s: [])

    def fake_upsert(client, name, docs, mode="hybrid", **kw):
        calls.append(mode)
        return 0

    monkeypatch.setattr(index_mod.qb, "upsert_documents", fake_upsert)

    from recall.config import SourceConfig

    src = SourceConfig(name="lessons", path="$BRAIN_ROOT/m", glob="**/*.md", frontmatter="optional")
    index_mod.build_index([src], mode="sparse")
    assert calls == ["sparse"]


# --- 2. effective_mode helper + MCP honoring it ----------------------------

def test_effective_mode_precedence(monkeypatch):
    from recall.config import RankingConfig, effective_mode

    class _Cfg:
        ranking = RankingConfig(mode="hybrid")

    cfg = _Cfg()
    # config only
    monkeypatch.delenv("RECALL_MODE", raising=False)
    assert effective_mode(cfg) == "hybrid"
    # env overrides config
    monkeypatch.setenv("RECALL_MODE", "sparse")
    assert effective_mode(cfg) == "sparse"
    # explicit override beats env
    assert effective_mode(cfg, "dense") == "dense"


def test_mcp_handler_threads_mode(monkeypatch):
    from recall import mcp_server

    captured = {}

    class _Cfg:
        class ranking:
            mode = "sparse"
            embedder = "e"
            sparse_embedder = "se"
            reranker = "none"
            reranker_model = "rm"
            rerank_n = 20
            needs_review_policy = "demote"
            needs_review_penalty = 0.5
        sources = []

    monkeypatch.setattr(mcp_server, "load_config", lambda: _Cfg())
    monkeypatch.setattr(mcp_server, "needs_refresh", lambda s: True)
    monkeypatch.delenv("RECALL_MODE", raising=False)

    def fake_build_index(sources, mode="hybrid"):
        captured["build_mode"] = mode

        class _Cache:
            documents = [object()]
        return _Cache()

    class _FakeRetriever:
        def __init__(self, **kw):
            captured["retriever_mode"] = kw.get("mode")

        def query(self, *a, **k):
            return []

    monkeypatch.setattr(mcp_server, "build_index", fake_build_index)
    monkeypatch.setattr(mcp_server, "HybridRetriever", _FakeRetriever)
    monkeypatch.setattr(mcp_server, "serialize_results", lambda r: [])

    mcp_server.recall_query_handler("q")
    assert captured["build_mode"] == "sparse"
    assert captured["retriever_mode"] == "sparse"


# --- 3. doctor scanner finds the git root at the brain root ---------------

def test_secret_scanner_check_uses_git_root_not_memory_subdir(tmp_path, monkeypatch):
    from recall.cli import _check_secret_scanner

    # Standard layout: git repo at the brain root, memory/ is the resolved
    # brain home the doctor passes in.
    brain_root = tmp_path / ".agent"
    memory = brain_root / "memory"
    memory.mkdir(parents=True)
    (brain_root / ".git").mkdir()

    import subprocess

    def fake_run(cmd, **kw):
        class _R:
            returncode = 0
            stdout = b"git@example.com:me/brain.git\n"
        return _R()

    monkeypatch.setattr(subprocess, "run", fake_run)

    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)  # no scanner

    notes: list[str] = []
    issues: list[str] = []
    # Pass the memory subdir, as doctor does. The check must still find .git
    # at the parent and raise the missing-scanner issue.
    _check_secret_scanner(memory, notes, issues)
    assert any("install-scanner" in i or "scanner" in i.lower() for i in issues), (
        f"expected a missing-scanner issue; got issues={issues} notes={notes}"
    )


def test_secret_scanner_check_noop_without_git(tmp_path, monkeypatch):
    from recall.cli import _check_secret_scanner

    memory = tmp_path / ".agent" / "memory"
    memory.mkdir(parents=True)
    notes: list[str] = []
    issues: list[str] = []
    _check_secret_scanner(memory, notes, issues)
    # No git anywhere -> no issue, no crash.
    assert issues == []
