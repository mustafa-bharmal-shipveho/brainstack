"""MCP server wrapper around the recall retriever.

Exposes a single tool, `recall_query`, with the same JSON contract as the CLI.
Skipped silently in tests if the `mcp` library is not installed.
"""

from __future__ import annotations

import importlib.util
import json
import os
from typing import Optional

from recall.config import effective_mode, load_config
from recall.core import HybridRetriever
from recall.index import build_index, load_index, needs_refresh
from recall.serialize import serialize_results, wire_to_serialized

# Generous next to the hook's 800 ms budget: an MCP call is an explicit tool
# invocation the model is already waiting on, and the daemon's first query
# pays the embedder + cross-encoder load.
_DAEMON_BUDGET_MS = 5000


def _query_via_daemon(
    query: str, k: int, source: Optional[str], type: Optional[str]
) -> Optional[list[dict]]:
    """Try the warm daemon first. Returns None only when the daemon is DOWN
    (`no_socket` / `connection_refused`), so the caller falls through to the
    in-process path.

    Without this, every MCP query would hit "index is busy" the moment the
    user installs the daemon — it holds the embedded store's exclusive
    process lock for as long as it runs. That same lock is why a daemon
    that is UP but did not answer (`timeout`, `protocol_error`,
    `server_error`) must NOT fall back: the in-process path would block on
    the lock and fail with the same "index is busy", hiding the real cause.
    It raises instead, and the MCP layer reports the error to the caller.
    """
    if os.environ.get("RECALL_NO_DAEMON") == "1":
        return None
    try:
        from recall import daemon_client

        # `recall.config`, not `recall.daemon`: resolving a path must not
        # drag in `recall.index` -> `qdrant_client` (~0.9 s) on a code path
        # whose whole point is to avoid loading retrieval in this process.
        from recall.config import daemon_socket_path
    except ImportError:
        return None
    try:
        resp = daemon_client.query(
            query,
            k=k,
            socket_path=daemon_socket_path(),
            budget_ms=_DAEMON_BUDGET_MS,
            source_filter=source,
            type_filter=type,
        )
    except daemon_client.DaemonUnavailable as exc:
        if exc.reason in daemon_client.DAEMON_DOWN_REASONS:
            return None
        raise RuntimeError(
            f"recall daemon is running but did not answer ({exc.reason}): {exc}. "
            "It owns the index while it runs, so the query was not retried "
            "in-process. Retry shortly, or check `recall serve --status`."
        ) from exc
    return [wire_to_serialized(item) for item in (resp.get("results") or [])]


def recall_query_handler(
    query: str,
    k: int = 5,
    source: Optional[str] = None,
    type: Optional[str] = None,
) -> list[dict]:
    """The handler dispatched to by the MCP tool. Pure Python, no MCP deps.

    Routes through the warm daemon when one is running (it owns the store),
    and falls back to the in-process path otherwise.

    Does NOT close the embedded-Qdrant client between requests. The MCP
    server is long-lived; tearing down the QdrantClient after every query
    defeats the whole point of `_qdrant_client_singleton` (amortized
    RocksDB open + index scan). Cleanup happens on server shutdown via
    `qdrant_backend.close_client_cache`'s atexit registration.
    """
    routed = _query_via_daemon(query, k, source, type)
    if routed is not None:
        return routed

    cfg = load_config()
    # Honor the same retrieval mode the CLI/auto-recall use (RECALL_MODE env >
    # ranking.mode). Without this, a user who set sparse to avoid the dense
    # model would still trigger a dense download/load over MCP, on both the
    # index build and the query.
    mode = effective_mode(cfg)
    fresh = needs_refresh(cfg.sources)
    cache = build_index(cfg.sources, mode=mode) if fresh else load_index(cfg.sources)
    if cache is None or not cache.documents:
        return []
    retriever = HybridRetriever(
        documents=cache.documents if fresh else None,
        collections=[s.name for s in cfg.sources],
        embedder=cfg.ranking.embedder,
        sparse_embedder=cfg.ranking.sparse_embedder,
        reranker=cfg.ranking.reranker,
        reranker_model=cfg.ranking.reranker_model,
        rerank_n=cfg.ranking.rerank_n,
        mode=mode,
        # Seam fix (Codex review): the MCP path must honor the same
        # needs_review ranking policy as the CLI. Without these, a doc
        # flagged for review ranked at full strength over MCP even when
        # the user configured "exclude"/"demote".
        needs_review_policy=cfg.ranking.needs_review_policy,
        needs_review_penalty=cfg.ranking.needs_review_penalty,
    )
    results = retriever.query(query, k=k, type_filter=type, source_filter=source)
    return serialize_results(results)


# The one tool this server exposes. Read-only by construction: there is no
# remember/forget/pending tool, so an injected prompt cannot mutate the brain
# through the MCP seam. `inputSchema` is accepted by both mcp 1.x (field name)
# and 2.x (alias for `input_schema`).
_TOOL_NAME = "recall_query"
_TOOL_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "The search query"},
        "k": {
            "type": "integer",
            "description": "Number of results (default 5)",
            "default": 5,
        },
        "source": {"type": "string", "description": "Optional source name filter"},
        "type": {
            "type": "string",
            "description": (
                "Optional frontmatter type filter "
                "(feedback, user, project, reference)"
            ),
        },
    },
    "required": ["query"],
}
_TOOL_DESCRIPTION = (
    "Retrieve relevant memories from the configured brain(s). "
    "Returns top-k matches as JSON."
)


def _recall_tool():
    from mcp.types import Tool

    return Tool(
        name=_TOOL_NAME,
        description=_TOOL_DESCRIPTION,
        inputSchema=_TOOL_INPUT_SCHEMA,
    )


def _dispatch_tool_call(name: str, arguments: dict) -> list:
    """Shared tool body for both mcp generations."""
    from mcp.types import TextContent

    if name != _TOOL_NAME:
        raise ValueError(f"Unknown tool: {name}")
    results = recall_query_handler(
        query=arguments["query"],
        k=int(arguments.get("k", 5)),
        source=arguments.get("source"),
        type=arguments.get("type"),
    )
    return [TextContent(type="text", text=json.dumps(results, indent=2))]


class _ResultEnvelope:
    """The `ServerResult` shape mcp 1.x wrapped every handler result in.

    2.x dropped the wrapper (`ServerResult` became a plain union and handlers
    return the concrete result), but `.root` is the accessor anything that
    introspects `build_server()` reads. Keeping it stable is what lets one
    caller work against `mcp>=1.0` — the range `pyproject.toml` declares —
    instead of only whichever generation happens to be installed.
    """

    __slots__ = ("root",)

    def __init__(self, root):
        self.root = root


def _method_to_request_type() -> dict:
    """{"tools/list": ListToolsRequest, ...} from the client-request union."""
    import typing

    from mcp import types

    out = {}
    for member in typing.get_args(types.ClientRequest):
        field = getattr(member, "model_fields", {}).get("method")
        default = getattr(field, "default", None)
        if isinstance(default, str):
            out[default] = member
    return out


class _CompatibleServer:
    """mcp 2.x `Server` plus the 1.x `request_handlers` introspection view.

    Everything the protocol runtime touches is native 2.x — this only adds
    back a read-only accessor keyed by request TYPE, which is how callers ask
    "what does this server actually answer?" without opening a transport.
    Built as a subclass at call time because `Server` is only importable when
    the optional `mcp` extra is installed.
    """

    _cls = None

    @classmethod
    def wrap(cls, server_cls):
        if cls._cls is not None:
            return cls._cls

        class _Server(server_cls):
            @property
            def request_handlers(self) -> dict:
                by_method = _method_to_request_type()
                mapping: dict = {}
                for method, entry in self._request_handlers.items():
                    request_type = by_method.get(method)
                    if request_type is None:
                        continue
                    mapping[request_type] = _compat_handler(entry)
                return mapping

        cls._cls = _Server
        return _Server


def _compat_handler(entry):
    """Adapt a 2.x `(ctx, params) -> result` handler to 1.x's
    `(request) -> ServerResult`."""

    async def _call(request):
        result = await entry.handler(None, getattr(request, "params", None))
        return _ResultEnvelope(result)

    return _call


def build_server():
    """Construct an MCP server with the recall_query tool registered.

    Supports both mcp generations, because `pyproject.toml` declares
    `mcp>=1.0` and either can be resolved: 2.x takes handlers as constructor
    arguments (`on_list_tools` / `on_call_tool`), 1.x registers them through
    decorators.
    """
    if importlib.util.find_spec("mcp") is None:
        raise RuntimeError(
            "The 'mcp' extra is not installed. Install with: pip install 'recall-brain[mcp]'"
        )

    import inspect

    from mcp.server import Server

    if "on_list_tools" in inspect.signature(Server.__init__).parameters:
        return _build_server_modern(Server)
    return _build_server_legacy(Server)


def _build_server_modern(server_cls):
    """mcp >= 2: constructor-based handler registration."""
    from mcp import types

    async def _on_list_tools(_ctx, _params):
        return types.ListToolsResult(tools=[_recall_tool()])

    async def _on_call_tool(_ctx, params):
        return types.CallToolResult(
            content=_dispatch_tool_call(params.name, params.arguments or {})
        )

    return _CompatibleServer.wrap(server_cls)(
        "recall-brain",
        on_list_tools=_on_list_tools,
        on_call_tool=_on_call_tool,
    )


def _build_server_legacy(server_cls):
    """mcp 1.x: decorator-based handler registration."""
    server = server_cls("recall-brain")

    @server.list_tools()
    async def _list_tools():
        return [_recall_tool()]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict):
        return _dispatch_tool_call(name, arguments)

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
