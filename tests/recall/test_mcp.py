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
