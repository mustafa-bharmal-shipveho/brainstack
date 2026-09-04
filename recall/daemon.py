"""Warm retrieval daemon (S3).

Holds one `HybridRetriever` (reranker loaded) and OWNS the embedded Qdrant
store, which is exclusively locked per process. Everything else — `recall
query`, `recall reindex`, `recall-mcp`, the Claude Code auto-recall hook —
has to reach retrieval through this AF_UNIX socket while the daemon runs.

Protocol (pinned; see docs/recall-daemon.md once it lands):

  * One NDJSON request per connection over `AF_UNIX`, socket mode 0600.
  * `handle_request` is the pure dispatch path — a dict in, a dict out, no
    socket required — so protocol shape is unit-testable without binding
    anything. `serve_forever` wraps it with the real
    `ThreadingMixIn` + `UnixStreamServer` socket lifecycle so concurrent
    hook fires are all answered.
  * Retrieval is serialized under one lock (`_query_lock`); a background
    thread runs `refresh_once()` (via `recall.index.refresh_index_chunked`)
    on `refresh_interval_s`, so the daemon — not the CLI — owns index
    freshness.

Scaffold: class/module shape + constants are real; every method body that
does actual dispatch, socket I/O, or freshness bookkeeping raises
`NotImplementedError("scaffold")` pending the Development phase. See
tests/recall/test_daemon.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Imported (not yet called — refresh_once() is still a scaffold stub) so
# `monkeypatch.setattr("recall.daemon.refresh_index_chunked", ...)` has a
# real module attribute to replace, matching the eventual call site.
from recall.index import refresh_index_chunked  # noqa: F401

# Wire protocol version. Bumping this is a breaking change for every
# client (the hook, the CLI, recall-mcp) — coordinate via docs/recall-daemon.md.
PROTOCOL_VERSION = 1

# Hard cap on a single incoming request line. Prevents a misbehaving or
# malicious local client from exhausting daemon memory with an unbounded
# read.
MAX_REQUEST_BYTES = 65536

# Per-result body cap on the wire. `content_sha256` still covers the FULL
# body (computed before truncation) — the session dedup store compares
# that hash, so a truncated-body hash would make every doc longer than the
# cap look "changed" on every prompt.
BODY_WIRE_CAP = 2000


def result_to_wire(result: Any) -> dict:
    """Project one retrieval result (a `recall.core.QueryResult`-shaped
    object) onto the pinned wire dict: `path, source, title, name, type,
    description, score, rerank_score, provenance, frontmatter (whitelisted),
    body (capped), content_sha256 (of the FULL body)`.

    Scaffold: signature + docstring only. See
    tests/recall/test_daemon.py::test_result_to_wire_body_cap_and_sha and
    ::test_frontmatter_whitelist.
    """
    raise NotImplementedError("scaffold")


class RecallDaemon:
    """Warm retrieval server: one retriever, one Qdrant store, one socket.

    Construction is cheap and side-effect-light (records config; does not
    bind the socket or start the refresh loop) so tests can build an
    instance and drive `handle_request` directly without any I/O.
    """

    def __init__(
        self,
        *,
        socket_path: Path,
        cfg: "Any | None" = None,
        retriever: "Any | None" = None,
        rerank: bool = True,
        idle_timeout_s: float = 0.0,
        refresh_interval_s: float = 300.0,
    ):
        self.socket_path = Path(socket_path)
        self.cfg = cfg
        self.retriever = retriever
        self.rerank = rerank
        self.idle_timeout_s = idle_timeout_s
        self.refresh_interval_s = refresh_interval_s

        # Freshness state (S3). Real initial values — read directly by
        # tests before any refresh has run, so these are NOT stubs.
        self.last_refresh_ts: "float | None" = None
        self.last_refresh_ok: "bool | None" = None
        self.last_refresh_error: "str | None" = None
        self.last_refresh_ms: int = 0
        self.last_refresh_changed: int = 0
        self.last_refresh_deleted: int = 0
        self.refresh_pending: bool = False

        # Health counters read by the `status` op.
        self.queries_served: int = 0

    def handle_request(self, req: dict) -> dict:
        """Pure dispatch: a parsed request dict in, a response dict out.
        No socket I/O — this is the seam the pure-dispatch tests exercise.

        Scaffold: signature + docstring only. See
        tests/recall/test_daemon.py::test_handle_request_query_shape and
        ::test_handle_request_bad_op_and_bad_json.
        """
        raise NotImplementedError("scaffold")

    def serve_forever(self) -> None:
        """Bind the socket (replacing a stale file, refusing a second
        daemon on the same path), run the warm-up query, and serve
        connections on threads until `shutdown()`.

        Scaffold: signature + docstring only. See the socket-lifecycle
        tests in tests/recall/test_daemon.py.
        """
        raise NotImplementedError("scaffold")

    def shutdown(self) -> None:
        """Stop serving, unlink the socket file, return.

        Scaffold: signature + docstring only.
        """
        raise NotImplementedError("scaffold")

    def status(self) -> dict:
        """Health/status payload: pid, uptime, queries served, model info,
        and the freshness fields above.

        Scaffold: signature + docstring only. See
        tests/recall/test_daemon.py::test_status_op_reports_pid_and_queries.
        """
        raise NotImplementedError("scaffold")

    def refresh_once(self) -> dict:
        """Run one freshness pass via `recall.index.refresh_index_chunked`,
        updating the freshness state attributes above. Used by the
        background loop, the `reindex` op, and directly by tests.

        Scaffold: signature + docstring only. See the freshness tests in
        tests/recall/test_daemon.py.
        """
        raise NotImplementedError("scaffold")

    def index_stale(self) -> bool:
        """True when a query answered right now should be considered
        against a possibly-stale index: `refresh_pending or
        last_refresh_ok is False`.

        Scaffold: signature + docstring only.
        """
        raise NotImplementedError("scaffold")


def run_daemon(
    *,
    socket_path: Path,
    rerank: bool = True,
    idle_timeout_s: float = 0.0,
    refresh_interval_s: float = 300.0,
) -> int:
    """Entry point for `recall serve`: build a real `RecallDaemon` from the
    user's config and run it until shutdown. Returns a process exit code.

    Scaffold: signature + docstring only.
    """
    raise NotImplementedError("scaffold")


__all__ = [
    "PROTOCOL_VERSION",
    "MAX_REQUEST_BYTES",
    "BODY_WIRE_CAP",
    "RecallDaemon",
    "result_to_wire",
    "run_daemon",
]
