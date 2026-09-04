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
    freshness.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from recall import __version__

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
# so four concurrent hook fires legitimately queue: with the cross-encoder
# on CPU that is seconds, not milliseconds. Answering `busy` there would
# turn the daemon's own design into an error, and each client already
# bounds its own wait with `budget_ms`.
QUERY_QUEUE_TIMEOUT_S = 60.0

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
    import datetime as _dt

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
    from recall.sanitize import provenance_label, sanitize_untrusted

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


# ---------------------------------------------------------------------------
# Socket plumbing
# ---------------------------------------------------------------------------


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
        super().__init__(socket_path, handler)

    def server_bind(self) -> None:
        super().server_bind()
        # Owner-only: any local user could otherwise query the brain.
        os.chmod(self.server_address, 0o600)

    def handle_error(self, request, client_address) -> None:
        # One bad connection must never take the daemon down or spew a
        # traceback into the launchd stderr log on every fire.
        pass


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
                        response = owner.handle_request(req)
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
    ):
        self.socket_path = Path(socket_path)
        self.cfg = cfg
        self.retriever = retriever
        self.rerank = rerank
        self.reranker_model = reranker_model
        self.rerank_n = rerank_n
        self.idle_timeout_s = idle_timeout_s
        self.refresh_interval_s = refresh_interval_s
        self.chunk_size = chunk_size

        # Resolved when the retriever is built; reported by `status` so a
        # health check can compare what is running against what was
        # calibrated. `warmup_ms` is the cost this daemon paid ONCE so no
        # prompt has to.
        self.effective_reranker_model: "str | None" = reranker_model
        self.effective_rerank_n: "int | None" = rerank_n
        self.warmup_ms: "int | None" = None

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
        # True for the whole duration of a refresh pass. Distinguishes "the
        # lock is held by a refresh" (report `busy`) from "the lock is held
        # by another query" (queue behind it).
        self._refresh_in_flight = False
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
        try:
            from recall.config import RankingConfig

            if configured != getattr(RankingConfig(), field):
                return configured
        except Exception:  # noqa: BLE001 - defaults are best-effort
            return configured if configured is not None else daemon_default
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

    @staticmethod
    def _accepts_rerank(retriever) -> bool:
        """`HybridRetriever.query(rerank=)` arrives with slice D. Probe once
        so this daemon works against both signatures during the merge
        window."""
        try:
            return "rerank" in inspect.signature(retriever.query).parameters
        except (TypeError, ValueError):  # pragma: no cover - exotic callables
            return False

    def _model_info(self) -> dict:
        retriever = self.retriever
        cfg = self._cfg_cache
        embedder = getattr(retriever, "_embedder_model", None) or getattr(
            retriever, "_embedder", None
        )
        if not isinstance(embedder, str):
            embedder = cfg.ranking.embedder if cfg is not None else None
        reranker = getattr(retriever, "_reranker", None)
        if not isinstance(reranker, str):
            reranker = ("cross_encoder" if self.rerank else "none")

        # Prefer what the daemon actually resolved, then whatever the
        # injected retriever reports, then the calibrated default. A health
        # check compares this against the calibrated model, so a guess from
        # the config would be worse than useless.
        reranker_model = self.effective_reranker_model
        if not isinstance(reranker_model, str):
            reranker_model = getattr(retriever, "_reranker_model", None)
        if not isinstance(reranker_model, str):
            reranker_model = DAEMON_RERANKER_MODEL

        rerank_n = self.effective_rerank_n
        if not isinstance(rerank_n, int):
            rerank_n = getattr(retriever, "_rerank_n", None)
        if not isinstance(rerank_n, int):
            rerank_n = DAEMON_RERANK_N

        return {
            "embedder": embedder,
            "reranker": reranker,
            "reranker_model": reranker_model,
            "rerank_n": int(rerank_n),
        }

    def note_activity(self) -> None:
        self._last_activity = time.monotonic()

    # -- dispatch -----------------------------------------------------------

    def handle_request(self, req: dict) -> dict:
        """Pure dispatch: a parsed request dict in, a response dict out.
        No socket I/O — this is the seam the pure-dispatch tests exercise.

        Nothing escapes as an exception. An unhandled error here would kill
        the connection thread WITHOUT answering, so the client would sit
        blocked until its own budget expired — the slowest possible way to
        learn something went wrong.
        """
        try:
            return self._dispatch(req)
        except Exception as exc:  # noqa: BLE001 - reported to the client
            return _error("internal", f"{type(exc).__name__}: {exc}")

    def _dispatch(self, req: dict) -> dict:
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
            return self._op_query(req)
        if op == "status":
            return self.status()
        if op == "reindex":
            return self._op_reindex()
        if op == "shutdown":
            return {"v": PROTOCOL_VERSION, "ok": True, "stopping": True}
        return _error("bad_request", f"unknown op {op!r}")

    def _op_query(self, req: dict) -> dict:
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
        if not self._acquire_for_query():
            return _error(
                "busy",
                "retrieval lock is held by an index refresh; retry shortly",
            )
        try:
            retriever = self._ensure_retriever()
            kwargs: dict = {
                "k": k,
                "type_filter": type_filter,
                "source_filter": source_filter,
            }
            if self._accepts_rerank(retriever):
                kwargs["rerank"] = effective_rerank
            results = retriever.query(prompt, **kwargs)
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

    def _acquire_for_query(self) -> bool:
        """Take the retrieval lock, distinguishing a queue from a blockage.

        Two different waits share one lock. Another QUERY holding it is the
        design working — retrieval is serialized because embedded Qdrant is
        not thread-safe — so the caller queues, bounded by its own
        `budget_ms`. A REFRESH holding it past `QUERY_LOCK_TIMEOUT_S` is a
        blockage worth reporting, because the client can retry against an
        index that will be fresher.
        """
        if self._query_lock.acquire(timeout=QUERY_LOCK_TIMEOUT_S):
            return True
        if self._refresh_in_flight:
            return False
        return self._query_lock.acquire(timeout=QUERY_QUEUE_TIMEOUT_S)

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
        """Run one freshness pass via `recall.index.refresh_index_chunked`,
        updating the freshness state. Used by the background loop, the
        `reindex` op, and directly by tests.

        A failed pass is recorded, not raised: the daemon keeps answering
        queries but reports `index_stale`, which is strictly better than
        dying and letting launchd respawn into the same failure.
        """
        started = time.perf_counter()
        self._refresh_in_flight = True
        try:
            from recall.config import effective_mode

            mode = effective_mode(self._config())
            result = refresh_index_chunked(
                self._iter_sources(),
                mode=mode,
                lock=self._query_lock,
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
            self._refresh_in_flight = False

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
                raise RuntimeError(
                    f"recall serve: already running at {path}, but it did not "
                    f"answer a status probe ({exc.reason}: {exc}). Stop it "
                    f"with 'recall serve --stop' before starting another."
                ) from exc
        else:
            raise RuntimeError(
                f"recall serve: already running (pid {probe.get('pid')}) at {path}. "
                f"Use 'recall serve --stop' to stop it."
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
                    kwargs: dict = {"k": 1}
                    if self._accepts_rerank(retriever):
                        kwargs["rerank"] = self.rerank
                    retriever.query("warmup", **kwargs)
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
    """`qdrant_backend.dense_fallback_active()` arrives with slice D; treat
    its absence as "not degraded" rather than crashing a query."""
    try:
        from recall import qdrant_backend as qb

        probe = getattr(qb, "dense_fallback_active", None)
        return bool(probe()) if callable(probe) else False
    except Exception:  # noqa: BLE001
        return False


def resolve_daemon_socket(raw: "str | None" = None) -> Path:
    """Resolve the daemon socket path.

    Delegates to `recall.config.daemon_socket_path` (slice A) and falls back
    to an equivalent local resolution while that lands, so this slice works
    before and after the merge. Precedence is identical either way:
    `$RECALL_DAEMON_SOCKET` > `raw` > `$BRAIN_ROOT/runtime/recall.sock` >
    `$BRAIN_HOME`'s parent > `~/.agent/runtime/recall.sock`.
    """
    try:
        from recall.config import daemon_socket_path

        return Path(daemon_socket_path(raw))
    except (ImportError, AttributeError, NotImplementedError):
        pass

    env = os.environ.get("RECALL_DAEMON_SOCKET")
    if env:
        return Path(os.path.expanduser(os.path.expandvars(env)))
    if raw:
        return Path(os.path.expanduser(os.path.expandvars(raw)))
    brain_root_env = os.environ.get("BRAIN_ROOT")
    if brain_root_env:
        return Path(os.path.expanduser(brain_root_env)) / "runtime" / "recall.sock"
    brain_home = os.environ.get("BRAIN_HOME")
    if brain_home:
        return Path(os.path.expanduser(brain_home)).parent / "runtime" / "recall.sock"
    return Path(os.path.expanduser("~/.agent")) / "runtime" / "recall.sock"


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
    "RecallDaemon",
    "result_to_wire",
    "resolve_daemon_socket",
    "run_daemon",
]
