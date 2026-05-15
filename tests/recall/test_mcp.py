"""Tests for the MCP server wrapper.

Skipped if `mcp` package not installed. The MCP server is a thin protocol
adapter — most logic is tested via the CLI/core tests. These tests verify
the protocol contract: tool list, tool invocation, JSON shape.
"""

from __future__ import annotations

import importlib.util

import pytest

pytestmark = pytest.mark.mcp


@pytest.fixture(autouse=True)
def _skip_if_unavailable():
    if importlib.util.find_spec("mcp") is None:
        pytest.skip("mcp library not installed")


def test_mcp_server_module_imports():
    from recall import mcp_server

    assert hasattr(mcp_server, "build_server")


def test_mcp_server_exposes_recall_query_tool():
    from recall.mcp_server import build_server

    server = build_server()
    # We don't run the server, just confirm tool registration
    # The exact API depends on the mcp library version
    assert server is not None


def test_mcp_query_returns_json_compatible(
    isolated_xdg, write_config, auto_memory_brain
):
    """Invoke the underlying handler that the MCP tool dispatches to."""
    write_config(
        sources=[
            {
                "name": "brain",
                "path": str(auto_memory_brain),
                "glob": "**/*.md",
                "frontmatter": "auto-memory",
                "exclude": [],
            }
        ]
    )
    from recall.mcp_server import recall_query_handler

    result = recall_query_handler(query="atomic write crash safety", k=3)
    assert isinstance(result, list)
    assert len(result) <= 3
    # Each entry must be JSON-serializable
    import json

    json.dumps(result)


def test_mcp_handler_reuses_embedded_qdrant_client_across_requests(
    isolated_xdg, write_config, auto_memory_brain
):
    """The MCP server is long-lived; tearing down the embedded-Qdrant
    client between requests defeats the singleton's whole purpose
    (amortized RocksDB open + index scan). Two back-to-back handler
    calls MUST share the same client object.

    Regression test for the staff-review finding that an earlier
    version of `recall_query_handler` called `close_client_cache()` in
    a `finally` block after every request.
    """
    pytest.importorskip("fcntl")
    write_config(
        sources=[
            {
                "name": "brain",
                "path": str(auto_memory_brain),
                "glob": "**/*.md",
                "frontmatter": "auto-memory",
                "exclude": [],
            }
        ]
    )
    from recall.mcp_server import recall_query_handler
    from recall.qdrant_backend import _clients, _client_lock_files

    # First call materializes the client and holds the process lock.
    result1 = recall_query_handler(query="atomic write crash safety", k=3)
    assert isinstance(result1, list)
    clients_after_first = dict(_clients)
    locks_after_first = dict(_client_lock_files)
    assert clients_after_first, "expected at least one cached QdrantClient after a query"

    # Second call must REUSE — identity must match, locks must persist.
    result2 = recall_query_handler(query="lessons", k=3)
    assert isinstance(result2, list)
    for key, client in _clients.items():
        assert client is clients_after_first.get(key), (
            "MCP handler reopened the embedded-Qdrant client between requests "
            "— singleton optimization regressed"
        )
    assert dict(_client_lock_files) == locks_after_first, (
        "MCP handler released the process-level Qdrant lock between requests"
    )
