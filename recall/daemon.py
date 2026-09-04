"""Warm retrieval daemon (S3).

Holds one `HybridRetriever` and OWNS the embedded Qdrant
store, which is exclusively locked per process. Everything else — `recall
query`, `recall reindex`, `recall-mcp`, the Claude Code auto-recall hook —
has to reach retrieval through this AF_UNIX socket while the daemon runs.

Protocol (pinned; see docs/recall-daemon.md):

  * One NDJSON request per connection over `AF_UNIX`, socket mode 0600.
  * `handle_request` is the pure dispatch path — a dict in, a dict out, no
    socket required — so protocol shape is unit-testable without binding
    anything. `serve_forever` wraps it with the real
    `ThreadingMixIn` + `UnixStreamServer` socket lifecycle so concurrent
    hook fires are all answered.
  * Retrieval is serialized under one lock (`_query_lock`); a background
    thread runs `refresh_once()` (via `recall.index.refresh_index_chunked`)
    on `refresh_interval_s`, so the daemon — not the CLI — owns index
    freshness. Only ONE refresh pass runs at a time: the `reindex` op joins
    a pass already in flight instead of double-embedding the brain.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from recall import __version__
from recall.sanitize import provenance_label, sanitize_untrusted

# Imported (not called until refresh_once runs) so
# `monkeypatch.setattr("recall.daemon.refresh_index_chunked", ...)` has a
# real module attribute to replace, matching the eventual call site.
from recall.index import refresh_index_chunked

# Wire protocol version. Bumping this is a breaking change for every
# client (the hook, the CLI, recall-mcp) — coordinate via docs/recall-daemon.md.
PROTOCOL_VERSION = 1

# Hard cap on a single incoming request line. Prevents a misbehaving or
# malicious local client from exhausting daemon memory with an unbounded
# read. The cap is INCLUSIVE: a line of exactly this many bytes is legal.
MAX_REQUEST_BYTES = 65536

# Per-result body cap on the wire. `content_sha256` still covers the FULL
# body (computed before truncation) — the session dedup store compares
# that hash, so a truncated-body hash would make every doc longer than the
# cap look "changed" on every prompt.
BODY_WIRE_CAP = 2000

# Longest prompt the daemon will accept. Anything above this is a client
# bug, not a query; retrieval on it would be meaningless and slow.
MAX_PROMPT_CHARS = 20000

# Seconds to wait for a complete request line before giving up on a
# connection. Without it, a client that writes forever and never sends a
# newline holds a daemon thread open until the process dies.
RECV_TIMEOUT_S = 2.0

# How long a query waits for the retrieval lock before it considers a
# refresh pass to be in the way. A chunked refresh holds the lock for one
# chunk (~150-300 ms), so 2 s means only a genuinely slow pass trips it.
QUERY_LOCK_TIMEOUT_S = 2.0

# How long a query waits when the lock is simply held by ANOTHER QUERY.
# Retrieval is serialized on purpose (embedded Qdrant is not thread-safe),
# so a few concurrent hook fires legitimately queue: with the cross-encoder
# on CPU that is seconds, not milliseconds. Answering `busy` immediately
# would turn the daemon's own design into an error.
#
# But the wait has to be BOUNDED near the client's own budget. The hook
# gives up at 800 ms; a request thread that queues for a minute and then
# runs the query is pure waste — the answer goes to a socket nobody is
# reading, while it holds the lock against clients that are still waiting.
# 2 s is ~2x the hook budget. Override with `RECALL_DAEMON_QUEUE_TIMEOUT_S`
# (or the `queue_timeout_s` constructor argument); a client that knows it
# will wait longer may also send `budget_ms` on a `query` request, and the
# daemon then queues for up to 2x that, capped at
# `QUERY_QUEUE_TIMEOUT_MAX_S`.
QUERY_QUEUE_TIMEOUT_S = 2.0
QUERY_QUEUE_TIMEOUT_MAX_S = 60.0

# Granularity of the queue wait. Short enough that a client that hung up
# is noticed promptly, long enough not to spin.
QUERY_QUEUE_POLL_S = 0.1

# How long `recall serve` sleeps before exiting 1 when another daemon
# already owns the socket. Under launchd `KeepAlive` a bare exit 1 is
# respawned every `ThrottleInterval` (10 s) forever, so the log fills with
# the same refusal several times a minute. Sleeping first turns that into
# roughly two lines a minute. Override with `RECALL_DAEMON_BUSY_BACKOFF_S`
# (tests set 0).
ALREADY_RUNNING_BACKOFF_S = 30.0

# Reranking is OPT-IN on the warm path (calibrated 2026-09-04, S4).
#
# The ordering experiment found no measurable relevance gain from the
# cross-encoder, for 4-13x the latency: 0.47-1.4 s with the config default
# (`jinaai/jina-reranker-v1-turbo-en` over 20 candidates), against a
# 60-130 ms retrieval-only warm path. That blows the hook's 800 ms client
# budget outright — the prompt gets nothing injected AND pays the stall.
# So `recall serve` defaults to `--no-rerank`; `--rerank` turns it on for
# anyone who wants to measure it on their own brain.
#
# When it IS on, these are the calibrated defaults:
# `Xenova/ms-marco-MiniLM-L-6-v2` over 10 candidates measured p50
# 230-280 ms and scored better than the config default. They are defaults,
# not overrides — a user who explicitly set `ranking.reranker_model` /
# `ranking.rerank_n` still wins, and `--reranker-model` / `--rerank-n` beat
# both.
DAEMON_RERANKER_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
DAEMON_RERANK_N = 10

# Frontmatter keys allowed onto the wire. Frontmatter is
# attacker-influenceable (any ingested doc sets it) and ships straight into
# a model context, so this is an allowlist, not a denylist.
FRONTMATTER_WHITELIST = (
    "created_by",
    "source",
    "reviewed_by",
    "provenance",
    "created",
    "date",
    "created_at",
    "name",
    "type",
    "description",
    "needs_review",
)


# ---------------------------------------------------------------------------
# Wire projection
# ---------------------------------------------------------------------------


def _json_safe(value: Any) -> Any:
    """Coerce a frontmatter value to something `json.dumps` accepts.

    YAML's safe_load hands back `datetime.date` / `datetime.datetime` for
    unquoted `created:` fields, which would raise inside the response
    encoder and take the whole request down.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)


def result_to_wire(result: Any) -> dict:
    """Project one retrieval result (a `recall.core.QueryResult`-shaped
    object) onto the pinned wire dict: `path, source, title, name, type,
    description, score, rerank_score, provenance, frontmatter (whitelisted),
    body (capped), content_sha256 (of the FULL body)`.

    `content_sha256` hashes the full body, NOT the capped one. The session
    dedup store compares that hash to decide whether a doc changed since it
    was last injected; hashing the truncated body would make every edit past
    char `BODY_WIRE_CAP` invisible.
    """
    doc = result.document
    frontmatter = dict(getattr(doc, "frontmatter", None) or {})
    body = getattr(doc, "body", "") or ""

    rerank_score = getattr(result, "rerank_score", None)

    name = frontmatter.get("name") or getattr(doc, "title", "") or ""
    description = frontmatter.get("description") or ""

    return {
        "path": str(getattr(doc, "path", "")),
        "source": getattr(doc, "source", None),
        "title": getattr(doc, "title", None),
        # `name`/`description` are frontmatter-sourced and land in a model
        # context, so they get the same untrusted treatment the CLI's
        # `serialize_results` applies.
        "name": sanitize_untrusted(str(name), max_len=300, keep_newlines=False),
        "type": _json_safe(frontmatter.get("type")),
        "description": sanitize_untrusted(
            str(description), max_len=300, keep_newlines=False
        ),
        "score": float(result.score),
        "rerank_score": None if rerank_score is None else float(rerank_score),
        "provenance": provenance_label(frontmatter),
        "frontmatter": {
            key: _json_safe(frontmatter[key])
            for key in FRONTMATTER_WHITELIST
            if key in frontmatter
        },
        "body": body[:BODY_WIRE_CAP],
        "content_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }


def _error(error: str, message: str) -> dict:
    return {"v": PROTOCOL_VERSION, "ok": False, "error": error, "message": message}


def _env_seconds(name: str, default: float) -> float:
    """Read a float number of seconds from the environment, falling back to
    `default` on anything unparseable or negative. A typo in a launchd plist
    must not stop the daemon from starting."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _first_of_type(kind: type, *candidates):
    """The first candidate that really is a `kind`, else `None`.

    `_model_info` reports what is LOADED, falling back through
    progressively weaker sources; each step is the same "did this source
    actually give me a str/int?" test, and a source that gave back
    something else has to be skipped rather than reported.
    """
    for candidate in candidates:
        if isinstance(candidate, kind):
            return candidate
    return None


class DaemonAlreadyRunning(RuntimeError):
    """Another daemon already answers on this socket.

    A `RuntimeError` subclass so existing callers that catch `RuntimeError`
    keep working; distinct so `run_daemon` can back off before exiting,
    instead of letting launchd respawn into the same refusal every 10 s.

    The offending pid is carried in the MESSAGE, which is the only thing
    any caller does with it (print it, then back off).
    """


# ---------------------------------------------------------------------------
# Socket plumbing
# ---------------------------------------------------------------------------


def _peer_gone(conn: socket.socket) -> bool:
    """Whether the client has closed its end (best effort, never blocks).

    A zero-length `MSG_PEEK` read means EOF: the client gave up on its own
    budget and nothing is left to answer. Anything else — buffered bytes, a
    would-block — means the connection is still worth serving. Errors are
    treated as "gone", since a socket that cannot be peeked cannot be
    written either.
    """
    try:
        conn.setblocking(False)
        try:
            data = conn.recv(1, socket.MSG_PEEK)
        finally:
            conn.settimeout(RECV_TIMEOUT_S)
    except (BlockingIOError, InterruptedError, TimeoutError):
        return False
    except OSError:
        return True
    return data == b""


class _DaemonServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """Threaded so four hooks firing at once are all answered. Retrieval is
    still serialized under the daemon's single lock — embedded Qdrant is not
    thread-safe — but a single-threaded accept loop would stall a burst even
    on `status` calls."""

    # Daemon threads so the `shutdown` op (handled ON a request thread) can
    # close the server without joining itself.
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False
    request_queue_size = 16

    def __init__(self, socket_path: str, handler, *, owner: "RecallDaemon"):
        self.owner = owner
        self._logged_error_types: set[str] = set()
        self._error_log_lock = threading.Lock()
        super().__init__(socket_path, handler)

    def server_bind(self) -> None:
        # umask, not chmod-after-bind: `bind()` creates the socket file with
        # the process umask applied, so on a typical 0022 umask there is a
        # window between bind and chmod where any local user can connect and
        # query the brain. `~/.agent/runtime` is usually 0755, so the path is
        # reachable. Restoring the old umask matters — the daemon writes
        # other files (Qdrant storage) after this.
        old_umask = os.umask(0o077)
        try:
            super().server_bind()
        finally:
            os.umask(old_umask)
        # Belt and braces: umask can only REMOVE bits, so this is the only
        # thing that guarantees exactly 0600 whatever the file was created
        # with.
        os.chmod(self.server_address, 0o600)

    def handle_error(self, request, client_address) -> None:
        # One bad connection must never take the daemon down or spew a
        # traceback into the launchd stderr log on every fire — but a
        # PERSISTENT crash in `handle()` (outside `handle_request`, which
        # swallows its own) would otherwise be completely invisible. One
        # line per exception TYPE: enough to diagnose, bounded even if every
        # connection fails.
        exc_type, exc, _tb = sys.exc_info()
        name = getattr(exc_type, "__name__", None) or "unknown"
        with self._error_log_lock:
            if name in self._logged_error_types:
                return
            self._logged_error_types.add(name)
        RecallDaemon._log(
            f"connection handler raised {name}: {exc} "
            f"(further {name} errors are not logged)"
        )


class _RequestHandler(socketserver.BaseRequestHandler):
    """One NDJSON request per connection."""

    def handle(self) -> None:
        owner: RecallDaemon = self.server.owner
        conn: socket.socket = self.request
        conn.settimeout(RECV_TIMEOUT_S)

        buf = bytearray()
        response: dict | None = None
        req: dict | None = None

        try:
            while b"\n" not in buf:
                if len(buf) > MAX_REQUEST_BYTES:
                    response = _error(
                        "bad_request",
                        f"request exceeds MAX_REQUEST_BYTES ({MAX_REQUEST_BYTES})",
                    )
                    break
                try:
                    chunk = conn.recv(65536)
                except TimeoutError:
                    response = _error(
                        "bad_request",
                        f"no complete request line within {RECV_TIMEOUT_S}s",
                    )
                    break
                if not chunk:
                    return  # client hung up before sending anything
                buf.extend(chunk)

            if response is None:
                line, sep, _rest = bytes(buf).partition(b"\n")
                if len(line) + len(sep) > MAX_REQUEST_BYTES:
                    response = _error(
                        "bad_request",
                        f"request exceeds MAX_REQUEST_BYTES ({MAX_REQUEST_BYTES})",
                    )
                else:
                    try:
                        req = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError) as exc:
                        response = _error("bad_request", f"malformed JSON: {exc}")
                    else:
                        owner.note_activity()
                        response = owner.handle_request(
                            req, is_connected=lambda: not _peer_gone(conn)
                        )
        except OSError:
            return

        if response is None:  # pragma: no cover - defensive
            response = _error("internal", "no response produced")

        try:
            conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
        except OSError:
            return

        # Acknowledge first, then stop. `shutdown()` blocks until the accept
        # loop exits, so it runs off this thread.
        if (
            isinstance(req, dict)
            and req.get("op") == "shutdown"
            and response.get("ok") is True
        ):
            threading.Thread(
                target=owner.shutdown, daemon=True, name="recall-daemon-shutdown"
            ).start()


# ---------------------------------------------------------------------------
# The daemon
# ---------------------------------------------------------------------------


class _RefreshTrackedLock:
    """The retrieval lock, as handed to `refresh_index_chunked`.

    Identical semantics to the raw `threading.Lock` — it IS the same lock —
    plus a depth counter that is non-zero only while the refresh actually
    holds it. `refresh_index_chunked` deliberately does its slow discovery
    and `stat()` phase OUTSIDE the lock, so "a refresh pass is running" and
    "a refresh is blocking queries" are different facts. Reporting `busy` on
    the first is a lie: with a big brain the discovery phase alone is
    seconds, during which the lock is free and every wait is just other
    queries queueing.
    """

    def __init__(self, lock: threading.Lock, on_change: "Callable[[int], None]"):
        self._lock = lock
        self._on_change = on_change

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        acquired = self._lock.acquire(blocking, timeout)
        if acquired:
            self._on_change(1)
        return acquired

    def release(self) -> None:
        # Decrement BEFORE releasing: a query waiting on the lock must never
        # wake up, win the lock, and still see "a refresh holds it".
        self._on_change(-1)
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    def __enter__(self) -> "_RefreshTrackedLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> bool:
        self.release()
        return False


class _RefreshPass:
    """One in-flight `refresh_once` pass. Late callers wait on `done` and
    read `result` instead of starting a second pass over the same brain."""

    __slots__ = ("done", "result")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: "dict | None" = None


class RecallDaemon:
    """Warm retrieval server: one retriever, one Qdrant store, one socket.

    Construction is cheap and side-effect-light (records config; does not
    bind the socket or start the refresh loop) so tests can build an
    instance and drive `handle_request` directly without any I/O.

    `rerank` defaults to False: the cross-encoder measured no relevance gain
    for 4-13x the latency, and the warm path's whole point is answering
    inside the hook's budget. Pass `rerank=True` to opt in.
    """

    def __init__(
        self,
        *,
        socket_path: Path,
        cfg: "Any | None" = None,
        retriever: "Any | None" = None,
        rerank: bool = False,
        reranker_model: "str | None" = None,
        rerank_n: "int | None" = None,
        idle_timeout_s: float = 0.0,
        refresh_interval_s: float = 300.0,
        chunk_size: int = 4,
        queue_timeout_s: "float | None" = None,
    ):
        self.socket_path = Path(socket_path)
        self.retriever = retriever
        self.rerank = rerank
        self.reranker_model = reranker_model
        self.rerank_n = rerank_n
        self.idle_timeout_s = idle_timeout_s
        self.refresh_interval_s = refresh_interval_s
        self.chunk_size = chunk_size
        self.queue_timeout_s = (
            _env_seconds("RECALL_DAEMON_QUEUE_TIMEOUT_S", QUERY_QUEUE_TIMEOUT_S)
            if queue_timeout_s is None
            else max(0.0, float(queue_timeout_s))
        )

        # Resolved when the retriever is built; reported by `status` so a
        # health check can compare what is running against what was
        # calibrated. `warmup_ms` is the cost this daemon paid ONCE so no
        # prompt has to.
        self.effective_reranker_model: "str | None" = reranker_model
        self.effective_rerank_n: "int | None" = rerank_n
        self.warmup_ms: "int | None" = None
        # `_model_info`'s answer, memoised once a retriever exists. Nothing
        # rebuilds the retriever or renames its models after that, so the
        # dict cannot go stale — see `_model_info`.
        self._model_info_cache: "dict | None" = None

        # Freshness state (S3).
        self.last_refresh_ts: "float | None" = None
        self.last_refresh_ok: "bool | None" = None
        self.last_refresh_error: "str | None" = None
        self.last_refresh_ms: int = 0
        self.last_refresh_changed: int = 0
        self.last_refresh_deleted: int = 0
        self.refresh_pending: bool = False

        # Health counters read by the `status` op.
        self.queries_served: int = 0

        # Every embedded-Qdrant call — query, upsert, delete, meta scroll —
        # goes through this one lock. That is the ONLY reason a threaded
        # server is safe here.
        self._query_lock = threading.Lock()
        self._started_ts = time.time()
        self._last_success_ts: "float | None" = None
        # Non-zero ONLY while a refresh pass is actually holding the
        # retrieval lock (see `_RefreshTrackedLock`). That is what
        # distinguishes "the lock is held by a refresh" (report `busy`,
        # the client can usefully retry) from "the lock is held by another
        # query" (queue behind it, bounded).
        self._refresh_lock_depth = 0
        self._refresh_depth_lock = threading.Lock()
        # Mutual exclusion for refresh passes: the background loop and the
        # `reindex` op must never walk and embed the same brain twice at
        # once. Guards `_refresh_running`, the pass a late caller joins.
        self._refresh_gate = threading.Lock()
        self._refresh_running: "_RefreshPass | None" = None
        self._last_activity = time.monotonic()
        self._stop = threading.Event()
        self._server: "_DaemonServer | None" = None
        self._cfg_cache = cfg
        self._closed = False
        self._lifecycle_lock = threading.Lock()

    # -- config / retriever -------------------------------------------------

    def _config(self):
        if self._cfg_cache is None:
            from recall.config import load_config

            self._cfg_cache = load_config()
        return self._cfg_cache

    def _iter_sources(self) -> Iterator[Any]:
        """Lazy on purpose: `refresh_index_chunked` takes an `Iterable` and
        does its own `list()`, so a test that monkeypatches the pass never
        triggers a config read."""
        yield from self._config().sources

    @staticmethod
    def _warm_default(configured, field: str, daemon_default):
        """Config wins only where the user actually chose something.

        `RankingConfig`'s defaults describe the one-shot CLI path, where a
        1.4 s rerank is merely slow. On the warm path it is a budget
        violation, so an untouched config field falls through to the
        calibrated daemon default instead.
        """
        from recall.config import RankingConfig

        if configured != getattr(RankingConfig(), field):
            return configured
        return daemon_default

    def _ensure_retriever(self):
        if self.retriever is not None:
            return self.retriever
        from recall.config import effective_mode
        from recall.core import HybridRetriever

        cfg = self._config()
        reranker_model = self.reranker_model or self._warm_default(
            cfg.ranking.reranker_model, "reranker_model", DAEMON_RERANKER_MODEL
        )
        rerank_n = self.rerank_n or self._warm_default(
            cfg.ranking.rerank_n, "rerank_n", DAEMON_RERANK_N
        )
        self.effective_reranker_model = reranker_model
        self.effective_rerank_n = int(rerank_n)

        self.retriever = HybridRetriever(
            documents=None,
            collections=[s.name for s in cfg.sources],
            embedder=cfg.ranking.embedder,
            sparse_embedder=cfg.ranking.sparse_embedder,
            # The daemon is the ONE process that can afford to hold the
            # cross-encoder resident, which is the entire point of --rerank.
            reranker="cross_encoder" if self.rerank else "none",
            reranker_model=reranker_model,
            rerank_n=int(rerank_n),
            needs_review_policy=cfg.ranking.needs_review_policy,
            needs_review_penalty=cfg.ranking.needs_review_penalty,
            mode=effective_mode(cfg),
        )
        return self.retriever

    def _model_info(self) -> dict:
        """What is actually loaded, for `status()` and every query response.

        Memoised once a retriever exists: it is read on the hot response
        path, and nothing rebuilds the retriever or renames its models for
        the life of the process. Before the retriever is built the answer
        is still a guess from config, so THAT is not cached — `status()`
        on a daemon that has not served a query yet must not pin it.
        """
        if self._model_info_cache is not None:
            return self._model_info_cache

        retriever = self.retriever
        cfg = self._cfg_cache
        # `HybridRetriever` stores the resolved model names as `_dense_model`
        # / `_reranker` / `_reranker_model` / `_rerank_n` (recall/core.py).
        # Reading the real attributes matters: `status()` is what a health
        # check compares against the calibrated model, so falling back to the
        # config value would report what was ASKED for, not what is loaded.
        embedder = _first_of_type(str, getattr(retriever, "_dense_model", None))
        if embedder is None and cfg is not None:
            embedder = cfg.ranking.embedder
        reranker = _first_of_type(str, getattr(retriever, "_reranker", None))
        if reranker is None:
            reranker = "cross_encoder" if self.rerank else "none"

        # Prefer what the daemon actually resolved, then whatever the
        # injected retriever reports, then the calibrated default. A health
        # check compares this against the calibrated model, so a guess from
        # the config would be worse than useless.
        info = {
            "embedder": embedder,
            "reranker": reranker,
            "reranker_model": _first_of_type(
                str,
                self.effective_reranker_model,
                getattr(retriever, "_reranker_model", None),
                DAEMON_RERANKER_MODEL,
            ),
            "rerank_n": int(_first_of_type(
                int,
                self.effective_rerank_n,
                getattr(retriever, "_rerank_n", None),
                DAEMON_RERANK_N,
            )),
        }
        if retriever is not None:
            self._model_info_cache = info
        return info

    def note_activity(self) -> None:
        self._last_activity = time.monotonic()

    # -- dispatch -----------------------------------------------------------

    def handle_request(
        self, req: dict, *, is_connected: "Callable[[], bool] | None" = None
    ) -> dict:
        """Pure dispatch: a parsed request dict in, a response dict out.
        No socket I/O — this is the seam the pure-dispatch tests exercise.

        `is_connected` is an optional liveness probe supplied by the socket
        handler; a query that is still queued when its client hangs up is
        dropped rather than run for nobody. It is keyword-only and optional
        so `handle_request({...})` stays the pure, socket-free seam.

        Nothing escapes as an exception. An unhandled error here would kill
        the connection thread WITHOUT answering, so the client would sit
        blocked until its own budget expired — the slowest possible way to
        learn something went wrong.
        """
        try:
            return self._dispatch(req, is_connected=is_connected)
        except Exception as exc:  # noqa: BLE001 - reported to the client
            return _error("internal", f"{type(exc).__name__}: {exc}")

    def _dispatch(
        self, req: dict, *, is_connected: "Callable[[], bool] | None" = None
    ) -> dict:
        if not isinstance(req, dict):
            return _error("bad_request", "request must be a JSON object")
        version = req.get("v")
        if version != PROTOCOL_VERSION:
            return _error(
                "bad_request",
                f"unsupported protocol version {version!r} "
                f"(this daemon speaks v{PROTOCOL_VERSION})",
            )
        op = req.get("op")
        if op == "query":
            return self._op_query(req, is_connected=is_connected)
        if op == "status":
            return self.status()
        if op == "reindex":
            return self._op_reindex()
        if op == "shutdown":
            return {"v": PROTOCOL_VERSION, "ok": True, "stopping": True}
        return _error("bad_request", f"unknown op {op!r}")

    def _op_query(
        self, req: dict, *, is_connected: "Callable[[], bool] | None" = None
    ) -> dict:
        prompt = req.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            return _error("bad_request", "query requires a non-empty 'prompt' string")
        if len(prompt) > MAX_PROMPT_CHARS:
            return _error(
                "bad_request",
                f"prompt is {len(prompt)} chars; the limit is {MAX_PROMPT_CHARS}",
            )
        try:
            k = int(req.get("k") or 5)
        except (TypeError, ValueError):
            return _error("bad_request", "'k' must be an integer")
        if k <= 0:
            return _error("bad_request", "'k' must be positive")

        rerank = req.get("rerank")
        if rerank is not None and not isinstance(rerank, bool):
            return _error("bad_request", "'rerank' must be a boolean or null")
        effective_rerank = self.rerank if rerank is None else rerank

        source_filter = req.get("source")
        type_filter = req.get("type")

        started = time.perf_counter()
        busy = self._acquire_for_query(
            queue_timeout_s=self._queue_timeout_for(req),
            is_connected=is_connected,
        )
        if busy is not None:
            return _error("busy", busy)
        try:
            retriever = self._ensure_retriever()
            results = retriever.query(
                prompt,
                k=k,
                type_filter=type_filter,
                source_filter=source_filter,
                rerank=effective_rerank,
            )
            # Counted under the lock so concurrent queries cannot lose an
            # increment, and only on success — `queries_served` means
            # "queries answered", not "queries attempted".
            self.queries_served += 1
        except Exception as exc:  # noqa: BLE001 - reported to the client
            return _error("internal", f"{type(exc).__name__}: {exc}")
        finally:
            self._query_lock.release()

        query_ms = int((time.perf_counter() - started) * 1000)
        model = self._model_info()
        return {
            "v": PROTOCOL_VERSION,
            "ok": True,
            "results": [result_to_wire(r) for r in results],
            "query_ms": query_ms,
            "degraded": _dense_fallback_active(),
            "reranked": bool(effective_rerank),
            "index_stale": self.index_stale(),
            "model": {"embedder": model["embedder"], "reranker": model["reranker"]},
        }

    def _note_refresh_lock(self, delta: int) -> None:
        with self._refresh_depth_lock:
            self._refresh_lock_depth += delta

    def _refresh_holds_lock(self) -> bool:
        with self._refresh_depth_lock:
            return self._refresh_lock_depth > 0

    def _queue_timeout_for(self, req: dict) -> float:
        """How long this request may queue behind other queries.

        The default is `queue_timeout_s` (~2x the hook's budget). A client
        that knows it will wait longer — `recall query` allows 60 s — may
        say so with `budget_ms`, and the daemon then queues for up to twice
        that. The daemon never waits materially longer than the client will:
        finishing a query nobody is reading is pure lock contention.
        """
        budget_ms = req.get("budget_ms")
        if isinstance(budget_ms, bool) or not isinstance(budget_ms, (int, float)):
            return self.queue_timeout_s
        if budget_ms <= 0:
            return self.queue_timeout_s
        client = min(2.0 * (float(budget_ms) / 1000.0), QUERY_QUEUE_TIMEOUT_MAX_S)
        return max(self.queue_timeout_s, client)

    def _acquire_for_query(
        self,
        *,
        queue_timeout_s: float,
        is_connected: "Callable[[], bool] | None" = None,
    ) -> "str | None":
        """Take the retrieval lock. Returns None on success, else the `busy`
        message explaining what the wait was actually blocked on.

        Two different waits share one lock, and the difference is reported
        HONESTLY — the flag is set only while a refresh really holds the
        lock, never for the discovery phase it runs lock-free:

          * A REFRESH holding it past `QUERY_LOCK_TIMEOUT_S` is a blockage
            worth reporting: the client can retry against a fresher index.
          * Another QUERY holding it is the design working (embedded Qdrant
            is not thread-safe), so the caller queues — but only for
            `queue_timeout_s`, near the client's own budget, and not at all
            once the client has hung up.
        """
        if self._query_lock.acquire(timeout=QUERY_LOCK_TIMEOUT_S):
            return None
        if self._refresh_holds_lock():
            return "retrieval lock is held by an index refresh; retry shortly"

        deadline = time.monotonic() + max(0.0, queue_timeout_s)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if is_connected is not None and not is_connected():
                return "client closed the connection while queued; request dropped"
            if self._query_lock.acquire(timeout=min(QUERY_QUEUE_POLL_S, remaining)):
                return None
            if self._refresh_holds_lock():
                return "retrieval lock is held by an index refresh; retry shortly"
        return (
            f"retrieval is serialized and another query still holds it after "
            f"{QUERY_LOCK_TIMEOUT_S + queue_timeout_s:g}s; retry shortly"
        )

    def _op_reindex(self) -> dict:
        result = self.refresh_once()
        if not result.get("ok", True):
            return _error("internal", str(result.get("error") or "refresh failed"))
        return {
            "v": PROTOCOL_VERSION,
            "ok": True,
            "changed": int(result.get("changed") or 0),
            "deleted": int(result.get("deleted") or 0),
            "ms": int(result.get("ms") or 0),
        }

    # -- health -------------------------------------------------------------

    def status(self) -> dict:
        """Health/status payload: pid, uptime, queries served, model info,
        and the freshness fields. This is the interface `recall doctor`,
        `recall serve --status`, and the S5 health planner all read."""
        model = self._model_info()
        cfg = self._cfg_cache
        collections: list[str] = []
        mode = None
        if cfg is not None:
            collections = [s.name for s in cfg.sources]
            # `effective_mode`, not `cfg.ranking.mode`: the retriever was
            # built with the effective mode ($RECALL_MODE beats the config
            # file), so reporting the raw config value would tell a health
            # check the daemon is running something it is not.
            try:
                from recall.config import effective_mode

                mode = effective_mode(cfg)
            except Exception:  # noqa: BLE001 - status must never raise
                mode = cfg.ranking.mode
        index_age_s = (
            None
            if self._last_success_ts is None
            else max(0.0, time.time() - self._last_success_ts)
        )
        return {
            "v": PROTOCOL_VERSION,
            "ok": True,
            "pid": os.getpid(),
            "uptime_s": max(0.0, time.time() - self._started_ts),
            "queries_served": self.queries_served,
            "socket": str(self.socket_path),
            "rerank": bool(self.rerank),
            "reranker_model": model["reranker_model"],
            "rerank_n": model["rerank_n"],
            "warmup_ms": self.warmup_ms,
            "embedder": model["embedder"],
            "mode": mode,
            "collections": collections,
            "degraded": _dense_fallback_active(),
            "version": __version__,
            "index_age_s": index_age_s,
            "last_refresh_ok": self.last_refresh_ok,
            "last_refresh_error": self.last_refresh_error,
            "last_refresh_ts": self.last_refresh_ts,
            "last_refresh_ms": self.last_refresh_ms,
            "last_refresh_changed": self.last_refresh_changed,
            "last_refresh_deleted": self.last_refresh_deleted,
            "refresh_pending": self.refresh_pending,
            "refresh_interval_s": float(self.refresh_interval_s),
        }

    def index_stale(self) -> bool:
        """True when a query answered right now should be considered against
        a possibly-stale index."""
        return bool(self.refresh_pending or self.last_refresh_ok is False)

    # -- freshness ----------------------------------------------------------

    def refresh_once(self) -> dict:
        """Run one freshness pass, or join the one already running.

        Passes are MUTUALLY EXCLUSIVE. The background loop and the `reindex`
        op (i.e. `recall reindex`, possibly twice) would otherwise walk and
        embed the whole brain concurrently, and — worse — the pass that
        finished second would clear `refresh_pending` while the first was
        still upserting, so `index_stale()` reported False in the middle of
        a refresh. A caller that arrives while a pass is in flight waits for
        it and gets ITS result, which is what `reindex` means anyway:
        "make sure the index is current before you answer me".
        """
        with self._refresh_gate:
            running = self._refresh_running
            if running is None:
                running = self._refresh_running = _RefreshPass()
                mine = running
            else:
                mine = None

        if mine is None:
            # Someone else's pass. Wait it out and report what it found.
            running.done.wait()
            if running.result is None:  # pragma: no cover - defensive
                return {
                    "ok": False,
                    "error": "refresh pass ended without a result",
                    "changed": 0,
                    "deleted": 0,
                    "ms": 0,
                }
            return dict(running.result)

        result = {
            "ok": False,
            "error": "refresh pass did not complete",
            "changed": 0,
            "deleted": 0,
            "ms": 0,
        }
        try:
            result = self._refresh_pass()
            return result
        finally:
            mine.result = result
            with self._refresh_gate:
                if self._refresh_running is mine:
                    self._refresh_running = None
            mine.done.set()

    def refresh_in_flight(self) -> bool:
        """Whether a refresh pass is running (lock held or not)."""
        with self._refresh_gate:
            return self._refresh_running is not None

    def _refresh_pass(self) -> dict:
        """One freshness pass via `recall.index.refresh_index_chunked`,
        updating the freshness state. Callers go through `refresh_once`,
        which serializes passes.

        A failed pass is recorded, not raised: the daemon keeps answering
        queries but reports `index_stale`, which is strictly better than
        dying and letting launchd respawn into the same failure.
        """
        started = time.perf_counter()
        try:
            from recall.config import effective_mode

            mode = effective_mode(self._config())
            result = refresh_index_chunked(
                self._iter_sources(),
                mode=mode,
                # Wrapped, not raw: the wrapper marks the windows where the
                # refresh actually holds the lock, so a query that waits
                # behind ANOTHER QUERY is never told an index refresh is in
                # the way.
                lock=_RefreshTrackedLock(self._query_lock, self._note_refresh_lock),
                chunk_size=self.chunk_size,
                on_pending=self._set_refresh_pending,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced via status/index_stale
            self.last_refresh_ok = False
            self.last_refresh_error = f"{type(exc).__name__}: {exc}"
            self.last_refresh_ts = time.time()
            self.last_refresh_ms = int((time.perf_counter() - started) * 1000)
            return {
                "ok": False,
                "error": self.last_refresh_error,
                "changed": 0,
                "deleted": 0,
                "ms": self.last_refresh_ms,
            }
        finally:
            self.refresh_pending = False

        self.last_refresh_ok = True
        self.last_refresh_error = None
        self.last_refresh_changed = int(result.changed)
        self.last_refresh_deleted = int(result.deleted)
        self.last_refresh_ms = int(result.ms)
        self.last_refresh_ts = time.time()
        self._last_success_ts = self.last_refresh_ts
        return {
            "ok": True,
            "changed": self.last_refresh_changed,
            "deleted": self.last_refresh_deleted,
            "ms": self.last_refresh_ms,
            "stale_before": bool(result.stale_before),
            "per_source": dict(result.per_source),
        }

    def _set_refresh_pending(self, pending: bool) -> None:
        self.refresh_pending = bool(pending)

    def _refresh_loop(self) -> None:
        interval = float(self.refresh_interval_s)
        if interval <= 0:
            return
        while not self._stop.is_set():
            self.refresh_once()
            if self._stop.wait(interval):
                return

    def _idle_loop(self) -> None:
        timeout = float(self.idle_timeout_s)
        if timeout <= 0:
            return
        while not self._stop.wait(min(1.0, timeout)):
            if time.monotonic() - self._last_activity >= timeout:
                self._log(f"idle for {timeout:.0f}s; shutting down")
                self.shutdown()
                return

    # -- lifecycle ----------------------------------------------------------

    @staticmethod
    def _log(message: str) -> None:
        print(f"recall serve: {message}", flush=True)

    def _prepare_socket(self) -> None:
        """Refuse when a daemon already answers here; otherwise clear a
        stale file left by a hard kill.

        Order matters: probe FIRST. Unlinking before probing would let a
        second daemon steal a live daemon's socket, leaving two owners of an
        exclusively locked store and every hook talking to whichever won.
        """
        from recall import daemon_client

        path = self.socket_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if not (path.exists() or path.is_symlink()):
            return

        try:
            probe = daemon_client.request(
                path, {"v": PROTOCOL_VERSION, "op": "status"}, budget_ms=1000
            )
        except daemon_client.DaemonUnavailable as exc:
            if exc.reason not in {"no_socket", "connection_refused"}:
                # SOMETHING is listening here — it just did not answer
                # cleanly. `status()` would flatten this to None and we would
                # unlink a live daemon's socket, leaving two processes
                # fighting over an exclusively locked store.
                raise DaemonAlreadyRunning(
                    f"recall serve: already running at {path}, but it did not "
                    f"answer a status probe ({exc.reason}: {exc}). Stop it "
                    f"with 'recall serve --stop' before starting another."
                ) from exc
        else:
            pid = probe.get("pid")
            raise DaemonAlreadyRunning(
                f"recall serve: already running (pid {pid}) at {path}. "
                f"Use 'recall serve --stop' to stop it, or "
                f"'kill {pid}' if it is a manual daemon you forgot about."
            )

        # Only reachable via no_socket / connection_refused: the file is a
        # leftover from a hard kill, so clearing it is safe.
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise RuntimeError(
                f"recall serve: cannot replace stale socket at {path}: {exc}"
            ) from exc

    def _unlink_socket(self) -> None:
        try:
            self.socket_path.unlink()
        except (FileNotFoundError, OSError):
            pass

    def serve_forever(self) -> None:
        """Bind the socket (replacing a stale file, refusing a second
        daemon on the same path), run the warm-up query, and serve
        connections on threads until `shutdown()`."""
        started = time.perf_counter()
        self._prepare_socket()

        server = _DaemonServer(str(self.socket_path), _RequestHandler, owner=self)
        self._server = server
        self._started_ts = time.time()
        self.note_activity()

        try:
            # Warm-up: pay the embedder + cross-encoder load ONCE, here,
            # instead of on the first user prompt. Held under the retrieval
            # lock so a client that connects mid-warm-up waits (or gets a
            # `busy`) rather than racing a half-built retriever.
            warm_ok = True
            warm_started = time.perf_counter()
            with self._query_lock:
                try:
                    retriever = self._ensure_retriever()
                    retriever.query("warmup", k=1, rerank=self.rerank)
                except Exception as exc:  # noqa: BLE001 - logged, not fatal
                    warm_ok = False
                    self._log(f"warm-up query failed: {type(exc).__name__}: {exc}")
            self.warmup_ms = int((time.perf_counter() - warm_started) * 1000)

            if self.refresh_interval_s and float(self.refresh_interval_s) > 0:
                threading.Thread(
                    target=self._refresh_loop,
                    daemon=True,
                    name="recall-daemon-refresh",
                ).start()
            if self.idle_timeout_s and float(self.idle_timeout_s) > 0:
                threading.Thread(
                    target=self._idle_loop, daemon=True, name="recall-daemon-idle"
                ).start()

            rerank_note = (
                f"on ({self.effective_reranker_model}, n={self.effective_rerank_n})"
                if self.rerank
                else "off"
            )
            self._log(
                f"ready in {int((time.perf_counter() - started) * 1000)} ms "
                f"(socket={self.socket_path}, rerank={rerank_note}, "
                f"warmup={self.warmup_ms} ms, warm={'ok' if warm_ok else 'failed'}, "
                f"refresh_interval_s={float(self.refresh_interval_s):g})"
            )
            server.serve_forever(poll_interval=0.2)
        finally:
            self._stop.set()
            with self._lifecycle_lock:
                self._closed = True
                try:
                    server.server_close()
                except Exception:  # noqa: BLE001
                    pass
            self._unlink_socket()

    def shutdown(self) -> None:
        """Stop serving, unlink the socket file, return. Idempotent, and
        safe to call on a daemon that never started."""
        self._stop.set()
        server = self._server
        if server is not None:
            try:
                server.shutdown()
            except Exception:  # noqa: BLE001
                pass
            with self._lifecycle_lock:
                if not self._closed:
                    self._closed = True
                    try:
                        server.server_close()
                    except Exception:  # noqa: BLE001
                        pass
            self._unlink_socket()


def _dense_fallback_active() -> bool:
    """Whether this process is answering from the BM25-only fallback because
    the dense embedder was unavailable. Reported as `degraded` on every
    query and in `status`; a probe failure must never take a query down."""
    try:
        from recall.qdrant_backend import dense_fallback_active

        return bool(dense_fallback_active())
    except Exception:  # noqa: BLE001 - degraded reporting is best-effort
        return False


def run_daemon(
    *,
    socket_path: Path,
    rerank: bool = False,
    reranker_model: "str | None" = None,
    rerank_n: "int | None" = None,
    idle_timeout_s: float = 0.0,
    refresh_interval_s: float = 300.0,
) -> int:
    """Entry point for `recall serve`: build a real `RecallDaemon` from the
    user's config and run it until shutdown. Returns a process exit code."""
    import signal

    # A pass every 5 minutes is the contract the freshness design was sized
    # for; anything longer means a memory written by the dream cycle sits
    # un-queryable for however long the user typed.
    interval = float(refresh_interval_s)
    if interval > 300.0:
        interval = 300.0

    daemon = RecallDaemon(
        socket_path=Path(socket_path),
        rerank=rerank,
        reranker_model=reranker_model,
        rerank_n=rerank_n,
        idle_timeout_s=idle_timeout_s,
        refresh_interval_s=interval,
    )

    def _on_signal(signum, _frame):  # pragma: no cover - signal path
        daemon._log(f"received signal {signum}; shutting down")
        threading.Thread(target=daemon.shutdown, daemon=True).start()

    # Only the main thread can install handlers; `recall serve` always runs
    # there, but guard so an embedded caller does not blow up.
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):  # pragma: no cover
            pass

    try:
        daemon.serve_forever()
    except DaemonAlreadyRunning as exc:
        print(str(exc), file=sys.stderr, flush=True)
        # launchd's KeepAlive respawns us every ThrottleInterval (10 s). If
        # a manual `recall serve` owns the socket, exiting straight away
        # means the same refusal six times a minute in
        # recall-daemon.stderr.log until someone notices. Sleep first: the
        # message still lands, roughly twice a minute.
        backoff = _env_seconds(
            "RECALL_DAEMON_BUSY_BACKOFF_S", ALREADY_RUNNING_BACKOFF_S
        )
        if backoff > 0:
            print(
                f"recall serve: sleeping {backoff:g}s before exiting so a "
                f"KeepAlive respawn does not repeat this every few seconds.",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(backoff)
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 1
    except OSError as exc:
        print(f"recall serve: cannot bind {socket_path}: {exc}", file=sys.stderr)
        return 1
    return 0


__all__ = [
    "PROTOCOL_VERSION",
    "MAX_REQUEST_BYTES",
    "BODY_WIRE_CAP",
    "FRONTMATTER_WHITELIST",
    "QUERY_QUEUE_TIMEOUT_S",
    "DaemonAlreadyRunning",
    "RecallDaemon",
    "result_to_wire",
    "run_daemon",
]
