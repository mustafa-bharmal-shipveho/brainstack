"""MCP server wrapper around the recall retriever.

Exposes a single tool, `recall_query`, with the same JSON contract as the CLI.
Skipped silently in tests if the `mcp` library is not installed.
"""

from __future__ import annotations

import importlib.util
import json
from typing import Optional

from recall.config import load_config
from recall.core import HybridRetriever
from recall.index import build_index, load_index, needs_refresh
from recall.serialize import serialize_results


def recall_query_handler(
    query: str,
    k: int = 5,
    source: Optional[str] = None,
    type: Optional[str] = None,
) -> list[dict]:
    """The handler dispatched to by the MCP tool. Pure Python, no MCP deps."""
    cfg = load_config()
    cache = build_index(cfg.sources) if needs_refresh(cfg.sources) else load_index(cfg.sources)
    if cache is None or not cache.documents:
        return []
    retriever = HybridRetriever(
        cache.documents,
        bm25_weight=cfg.ranking.bm25_weight,
        embedding_weight=cfg.ranking.embedding_weight,
        embedding_model=cfg.ranking.embedding_model,
    )
    results = retriever.query(query, k=k, type_filter=type, source_filter=source)
    return serialize_results(results)


def build_server():
    """Construct an MCP server with the recall_query tool registered."""
    if importlib.util.find_spec("mcp") is None:
        raise RuntimeError(
            "The 'mcp' extra is not installed. Install with: pip install 'recall-brain[mcp]'"
        )

    from mcp.server import Server
    from mcp.types import Tool, TextContent

    server = Server("recall-brain")

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return [
            Tool(
                name="recall_query",
                description=(
                    "Retrieve relevant memories from the configured brain(s). "
                    "Returns top-k matches as JSON."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "The search query"},
                        "k": {
                            "type": "integer",
                            "description": "Number of results (default 5)",
                            "default": 5,
                        },
                        "source": {
                            "type": "string",
                            "description": "Optional source name filter",
                        },
                        "type": {
                            "type": "string",
                            "description": "Optional frontmatter type filter (feedback, user, project, reference)",
                        },
                    },
                    "required": ["query"],
                },
            )
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> list[TextContent]:
        if name != "recall_query":
            raise ValueError(f"Unknown tool: {name}")
        results = recall_query_handler(
            query=arguments["query"],
            k=int(arguments.get("k", 5)),
            source=arguments.get("source"),
            type=arguments.get("type"),
        )
        return [TextContent(type="text", text=json.dumps(results, indent=2))]

    return server


def main():
    """Entry point for the recall-mcp script."""
    import asyncio

    from mcp.server.stdio import stdio_server

    server = build_server()

    async def _run():
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":
    main()
