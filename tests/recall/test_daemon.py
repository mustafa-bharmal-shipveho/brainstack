"""Warm retrieval daemon (S3): wire protocol, socket lifecycle, freshness.

The daemon holds one `HybridRetriever` (reranker loaded) and OWNS the
embedded Qdrant store, which is exclusively locked per process. Everything
else — `recall query`, `recall reindex`, `recall-mcp`, the Claude Code
auto-recall hook — has to reach retrieval through this socket while it runs,
so the wire shape here is a contract, not an implementation detail.

Two axes are pinned:

  * **Protocol.** One NDJSON request per connection over AF_UNIX. Result
    dicts carry exactly the keys in `WIRE_KEYS`; bodies are capped at
    `BODY_WIRE_CAP` chars while `content_sha256` still covers the FULL body
    (the dedup store compares that hash, so a truncated-body hash would make
    every long doc look changed on every prompt). Frontmatter is whitelisted
    because it is attacker-influenceable and ships straight into a model
    context.
  * **Freshness.** The daemon, not the CLI, refreshes the index. Failures
    have to be visible (`last_refresh_ok`, `last_refresh_error`,
    `index_stale`) rather than silently serving a stale brain.

Socket paths live under a short `/tmp` dir, never `tmp_path`: AF_UNIX
addresses are capped at 104 bytes on macOS and pytest's `tmp_path` blows
past that.

Hermetic by default. The `@pytest.mark.embeddings` tests at the bottom build
a real index and are excluded from `make test-ci`.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import stat
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest

from recall.daemon import (
    BODY_WIRE_CAP,
    MAX_REQUEST_BYTES,
    PROTOCOL_VERSION,
    RecallDaemon,
    result_to_wire,
)

# Every key a query result carries on the wire. Pinned: the CLI projects
# these onto `serialize_results`' shape and the hook's `normalize_results`
# reads them positionally by name. Adding a key is a protocol change.
WIRE_KEYS = {
    "path",
    "source",
    "title",
    "name",
    "type",
    "description",
    "score",
    "rerank_score",
    "provenance",
    "frontmatter",
    "body",
    "content_sha256",
}

# Frontmatter keys allowed through to the model context.
FRONTMATTER_WHITELIST = {
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
}

_UNSET = object()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeDoc:
    path: str
    source: str
    title: str
    frontmatter: dict
    body: str
    text: str


@dataclass
class _FakeResult:
    document: _FakeDoc
    score: float
    rerank_score: Optional[float] = None


def _doc(
    name: str = "atomic-writes",
    *,
    source: str = "brain",
    body: str = "Always write to a tmp file then os.replace it.",
    frontmatter: Optional[dict] = None,
    path: Optional[str] = None,
) -> _FakeDoc:
    fm = (
        {"name": name, "type": "feedback", "description": f"lesson: {name}"}
        if frontmatter is None
        else frontmatter
    )
    return _FakeDoc(
        path=path or f"/brain/semantic/lessons/{name}.md",
        source=source,
        title=name,
        frontmatter=fm,
        body=body,
        text=f"{name}\n{body}",
    )


def _results(n: int = 2) -> list[_FakeResult]:
    return [
        _FakeResult(
            document=_doc(f"lesson-{i}", body=f"body of lesson {i}"),
            score=1.0 - (i * 0.1),
            rerank_score=0.9 - (i * 0.1),
        )
        for i in range(n)
    ]


class _FakeRetriever:
    """Stands in for `HybridRetriever` without touching Qdrant or fastembed.

    Accepts the full call shape the daemon may use (`rerank=` lands with
    slice D) and records every call so tests can assert what was forwarded.
    """

    def __init__(
        self,
        results: Optional[list] = None,
        *,
        delay_s: float = 0.0,
        raises: Optional[BaseException] = None,
    ):
        self._results = list(results) if results is not None else _results()
        self._delay_s = delay_s
        self._raises = raises
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def query(
        self,
        query: str,
        k: int,
        type_filter: Optional[str] = None,
        source_filter: Optional[str] = None,
        rerank: Optional[bool] = None,
    ) -> list:
        with self._lock:
            self.calls.append(
                {
                    "query": query,
                    "k": k,
                    "type_filter": type_filter,
                    "source_filter": source_filter,
                    "rerank": rerank,
                }
            )
        if self._raises is not None:
            raise self._raises
        if self._delay_s:
            time.sleep(self._delay_s)
        return self._results[:k]


@dataclass
class _FakeRefresh:
    """Stand-in for `recall.index.refresh_index_chunked`.

    Drives the daemon's freshness state machine without embedding anything:
    calls `on_pending(True)` (so tests can observe `refresh_pending` mid-pass)
    and returns a `RefreshResult`, or raises to simulate a failed pass.
    """

    changed: int = 0
    deleted: int = 0
    ms: int = 7
    raises: Optional[BaseException] = None
    observed_pending: list = field(default_factory=list)
    calls: int = 0
    observer: Optional[object] = None

    def __call__(self, sources, **kwargs):
        from recall.index import RefreshResult

        self.calls += 1
        on_pending = kwargs.get("on_pending")
        if on_pending is not None:
            on_pending(True)
            if self.observer is not None:
                self.observed_pending.append(
                    getattr(self.observer, "refresh_pending", None)
                )
        if self.raises is not None:
            raise self.raises
        return RefreshResult(
            changed=self.changed,
            deleted=self.deleted,
            stale_before=bool(self.changed or self.deleted),
            ms=self.ms,
            per_source={"brain": self.changed},
        )


class _GatedRetriever:
    """A retriever whose queries block until the test lets them finish.

    Holding the retrieval lock for a controlled window is the only way to
    exercise the queueing path, and doing it with `time.sleep` in the fake
    would make every assertion a race against the scheduler. `entered` fires
    once a query is inside the lock; `release` lets it return.
    """

    def __init__(self, results: Optional[list] = None):
        self._results = list(results) if results is not None else _results(1)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self._lock = threading.Lock()

    def query(
        self,
        query: str,
        k: int,
        type_filter: Optional[str] = None,
        source_filter: Optional[str] = None,
        rerank: Optional[bool] = None,
    ) -> list:
        with self._lock:
            self.calls += 1
        self.entered.set()
        self.release.wait(30)
        return self._results[:k]


class _PhasedRefresh:
    """Stand-in for `refresh_index_chunked` with its REAL phase structure.

    The production pass walks the filesystem and stats every doc OUTSIDE the
    lock (seconds on a large brain), then takes the lock in short chunks.
    Collapsing that into "the pass holds the lock" is exactly the mistake
    that made every slow query blame an index refresh, so the fake keeps the
    two phases separate and lets the test drive each one.
    """

    def __init__(self, *, changed: int = 1, deleted: int = 0, ms: int = 5):
        self.changed = changed
        self.deleted = deleted
        self.ms = ms
        self.calls = 0
        self.discovery_entered = threading.Event()
        self.finish_discovery = threading.Event()
        self.lock_held = threading.Event()
        self.release_lock = threading.Event()
        self.second_call = threading.Event()
        self._lock = threading.Lock()

    def __call__(self, sources, **kwargs):
        from recall.index import RefreshResult

        with self._lock:
            self.calls += 1
            if self.calls > 1:
                self.second_call.set()
        on_pending = kwargs.get("on_pending")
        if on_pending is not None:
            on_pending(True)
        # Phase 1: discovery. Lock-free, and the slowest part of a real pass.
        self.discovery_entered.set()
        self.finish_discovery.wait(30)
        # Phase 2: the chunked upsert. The ONLY window that blocks a query.
        with kwargs["lock"]:
            self.lock_held.set()
            self.release_lock.wait(30)
        return RefreshResult(
            changed=self.changed,
            deleted=self.deleted,
            stale_before=bool(self.changed or self.deleted),
            ms=self.ms,
            per_source={"brain": self.changed},
        )


def _query_req(prompt: str = "atomic writes", k: int = 1, **extra) -> dict:
    return {"v": 1, "op": "query", "prompt": prompt, "k": k, **extra}


# ---------------------------------------------------------------------------
# Socket helpers
# ---------------------------------------------------------------------------


def _send(
    sock_path,
    payload: Optional[dict] = None,
    *,
    timeout: float = 5.0,
    raw: Optional[bytes] = None,
) -> Optional[dict]:
    """One NDJSON request per connection; returns the parsed response line."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(sock_path))
        s.sendall(raw if raw is not None else (json.dumps(payload) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        head = buf.split(b"\n", 1)[0].strip()
        if not head:
            return None
        return json.loads(head.decode())
    finally:
        s.close()


def _wait_for_socket(path: Path, *, timeout: float = 10.0, thread_state=None) -> None:
    """Block until the daemon ANSWERS, not merely until the socket file exists.

    `bind()` creates the file and `listen()` runs after it, so a client that
    connects in between gets `ConnectionRefusedError`. Waiting on
    `path.exists()` alone therefore hands out a socket that is not yet
    accepting, and under machine load that window is wide enough to fail a
    test that has nothing to do with startup. A real `status` round trip is
    the only honest readiness signal: it proves bind, listen, accept and the
    dispatch path all work.
    """
    from recall import daemon_client

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if thread_state is not None and thread_state.get("exc") is not None:
            raise AssertionError(
                f"daemon thread died before binding: {thread_state['exc']!r}"
            )
        if (
            path.exists()
            and stat.S_ISSOCK(os.stat(path).st_mode)
            and daemon_client.status(path, timeout_s=2.0) is not None
        ):
            return
        time.sleep(0.01)
    raise AssertionError(f"daemon never answered on {path} within {timeout}s")


def _wait_gone(path: Path, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"{path} still present after {timeout}s")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def short_sock_dir():
    """A short-enough dir for AF_UNIX (104-byte cap on macOS)."""
    d = tempfile.mkdtemp(prefix="rsd-", dir="/tmp")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def fake_daemon(short_sock_dir):
    """Factory: build a `RecallDaemon` over a fake retriever, serve in a thread.

    Returns `(daemon, socket_path, retriever)`. `refresh_interval_s` defaults
    to 0 so no background pass touches a real index. Pass `retriever=None`
    explicitly to let the daemon build a REAL one from config (embeddings
    tests only); pass `start=False` for pure `handle_request` tests.
    """
    started: list = []

    def _make(
        *,
        results=None,
        retriever=_UNSET,
        sock_name: str = "recall.sock",
        start: bool = True,
        **kwargs,
    ):
        sock = short_sock_dir / sock_name
        if retriever is _UNSET:
            retriever = _FakeRetriever(results)
        kwargs.setdefault("refresh_interval_s", 0.0)
        daemon = RecallDaemon(socket_path=sock, retriever=retriever, **kwargs)
        started.append(daemon)
        if start:
            state: dict = {"exc": None}

            def _serve():
                try:
                    daemon.serve_forever()
                except BaseException as exc:  # noqa: BLE001 - surfaced below
                    state["exc"] = exc

            t = threading.Thread(target=_serve, daemon=True, name="test-recall-daemon")
            t.start()
            _wait_for_socket(sock, thread_state=state)
        return daemon, sock, retriever

    yield _make

    for daemon in reversed(started):
        try:
            daemon.shutdown()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# handle_request — pure dispatch, no socket
# ---------------------------------------------------------------------------


def test_handle_request_query_shape(fake_daemon):
    """The pure dispatch path returns the pinned envelope and result keys."""
    daemon, _sock, retriever = fake_daemon(results=_results(2), start=False)

    resp = daemon.handle_request(
        {"v": 1, "op": "query", "prompt": "atomic writes", "k": 2}
    )

    assert resp["v"] == PROTOCOL_VERSION
    assert resp["ok"] is True
    assert isinstance(resp["query_ms"], int)
    assert isinstance(resp["degraded"], bool)
    assert isinstance(resp["reranked"], bool)
    assert isinstance(resp["index_stale"], bool)
    assert set(resp["model"]) >= {"embedder", "reranker"}

    assert len(resp["results"]) == 2
    for item in resp["results"]:
        assert set(item) == WIRE_KEYS, (
            f"wire result keys drifted from the pinned contract; "
            f"extra={set(item) - WIRE_KEYS} missing={WIRE_KEYS - set(item)}"
        )
        assert isinstance(item["score"], float)
        assert item["rerank_score"] is None or isinstance(item["rerank_score"], float)
        assert len(item["body"]) <= BODY_WIRE_CAP

    # The prompt and k reached the retriever unchanged.
    assert retriever.calls[-1]["query"] == "atomic writes"
    assert retriever.calls[-1]["k"] == 2


def test_handle_request_bad_op_and_bad_json(fake_daemon):
    """Every malformed request shape is a `bad_request`, never a crash."""
    daemon, sock, _retriever = fake_daemon()

    bad_requests = [
        {"v": 1, "op": "definitely-not-an-op"},
        {"v": 2, "op": "query", "prompt": "hi", "k": 1},
        {"v": 1, "op": "query", "k": 1},  # prompt missing
        {"v": 1, "op": "query", "prompt": "x" * 20001, "k": 1},  # prompt too long
    ]
    for req in bad_requests:
        resp = daemon.handle_request(req)
        assert resp["ok"] is False, f"expected rejection for {req.get('op')!r}: {resp}"
        assert resp["error"] == "bad_request", resp
        assert isinstance(resp.get("message"), str) and resp["message"], resp
        assert resp["v"] == PROTOCOL_VERSION

    # Bad JSON can only be exercised over the wire (handle_request takes a dict).
    resp = _send(sock, raw=b"{this is not json\n")
    assert resp is not None, "server closed without answering a garbage line"
    assert resp["ok"] is False
    assert resp["error"] == "bad_request"


def test_result_to_wire_body_cap_and_sha():
    """Body is truncated for the wire; the hash still covers the FULL body.

    The session dedup store compares `content_sha256` to decide whether a doc
    changed since it was last injected. Hashing the truncated body would make
    every doc longer than the cap look identical after an edit past char 2000.
    """
    body = "".join(f"line {i}\n" for i in range(2000))
    assert len(body) > BODY_WIRE_CAP
    result = _FakeResult(document=_doc("long", body=body), score=0.5, rerank_score=0.7)

    wire = result_to_wire(result)

    assert set(wire) == WIRE_KEYS
    assert len(wire["body"]) == BODY_WIRE_CAP
    assert wire["body"] == body[:BODY_WIRE_CAP]
    assert wire["content_sha256"] == hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert wire["content_sha256"] != hashlib.sha256(
        body[:BODY_WIRE_CAP].encode("utf-8")
    ).hexdigest()


def test_frontmatter_whitelist():
    """Only whitelisted frontmatter keys reach the model context."""
    frontmatter = {
        "name": "atomic-writes",
        "type": "feedback",
        "description": "write tmp then rename",
        "created_by": "mustafa",
        "source": "session-digest",
        "reviewed_by": "claude",
        "provenance": "llm-extracted",
        "created": "2026-08-01",
        "date": "2026-08-01",
        "created_at": "2026-08-01T10:00:00Z",
        "needs_review": False,
        # Not whitelisted — must be dropped.
        "internal_notes": "do not ship",
        "api_key": "sk-live-should-never-leave",
        "raw_transcript": "x" * 100,
    }
    result = _FakeResult(
        document=_doc("atomic-writes", frontmatter=frontmatter), score=0.4
    )

    wire = result_to_wire(result)

    assert set(wire["frontmatter"]) <= FRONTMATTER_WHITELIST, (
        f"non-whitelisted frontmatter leaked: "
        f"{set(wire['frontmatter']) - FRONTMATTER_WHITELIST}"
    )
    assert set(wire["frontmatter"]) == FRONTMATTER_WHITELIST
    assert "api_key" not in json.dumps(wire)
    assert "do not ship" not in json.dumps(wire)


# ---------------------------------------------------------------------------
# Socket lifecycle
# ---------------------------------------------------------------------------


def test_socket_round_trip_query(fake_daemon):
    """A real client over the real socket gets the same shape as the pure path."""
    _daemon, sock, retriever = fake_daemon(results=_results(3))

    resp = _send(sock, {"v": 1, "op": "query", "prompt": "atomic writes", "k": 3})

    assert resp["ok"] is True, resp
    assert resp["v"] == PROTOCOL_VERSION
    assert len(resp["results"]) == 3
    assert set(resp["results"][0]) == WIRE_KEYS
    assert retriever.calls[-1]["query"] == "atomic writes"


def test_socket_mode_is_0600(fake_daemon):
    """The socket is owner-only: any local user could otherwise query the brain."""
    _daemon, sock, _retriever = fake_daemon()

    mode = stat.S_IMODE(os.stat(sock).st_mode)
    assert mode == 0o600, f"socket mode is {oct(mode)}, expected 0o600"
    assert stat.S_ISSOCK(os.stat(sock).st_mode)


def test_status_op_reports_pid_and_queries(fake_daemon):
    """`status` is the health interface: pid plus a served-query counter."""
    _daemon, sock, _retriever = fake_daemon()

    before = _send(sock, {"v": 1, "op": "status"})
    assert before["ok"] is True, before
    assert before["pid"] == os.getpid()
    assert isinstance(before["queries_served"], int)
    assert isinstance(before["uptime_s"], (int, float))
    assert before["socket"] == str(sock)
    assert isinstance(before["rerank"], bool)
    for key in (
        "reranker_model",
        "embedder",
        "mode",
        "collections",
        "degraded",
        "version",
        "index_age_s",
        "last_refresh_ok",
        "last_refresh_error",
        "last_refresh_ts",
        "last_refresh_ms",
        "last_refresh_changed",
        "last_refresh_deleted",
        "refresh_pending",
        "refresh_interval_s",
    ):
        assert key in before, f"status is missing {key!r}: {before}"

    _send(sock, {"v": 1, "op": "query", "prompt": "one", "k": 1})
    _send(sock, {"v": 1, "op": "query", "prompt": "two", "k": 1})

    after = _send(sock, {"v": 1, "op": "status"})
    assert after["queries_served"] - before["queries_served"] == 2, (
        f"queries_served must count exactly the served queries: "
        f"{before['queries_served']} -> {after['queries_served']}"
    )


def test_shutdown_op_unlinks_socket(fake_daemon):
    """`shutdown` answers, exits, and leaves no stale socket file behind."""
    _daemon, sock, _retriever = fake_daemon()

    resp = _send(sock, {"v": 1, "op": "shutdown"})
    assert resp["ok"] is True, resp

    _wait_gone(sock)


def test_stale_socket_file_is_replaced(short_sock_dir, fake_daemon):
    """A leftover file at the socket path (hard kill, no cleanup) is unlinked.

    Without this, a crashed daemon permanently bricks the socket path and
    every hook fires on the slow in-process path forever.
    """
    sock = short_sock_dir / "recall.sock"
    sock.write_text("leftover from a killed daemon", encoding="utf-8")
    assert sock.exists() and not stat.S_ISSOCK(os.stat(sock).st_mode)

    _daemon, bound, _retriever = fake_daemon(sock_name="recall.sock")

    assert bound == sock
    assert stat.S_ISSOCK(os.stat(sock).st_mode)
    resp = _send(sock, {"v": 1, "op": "query", "prompt": "hello", "k": 1})
    assert resp["ok"] is True, resp


def test_second_daemon_refuses_when_running(fake_daemon):
    """Two daemons on one socket = two owners of an exclusively locked store."""
    _first, sock, _retriever = fake_daemon(sock_name="recall.sock")

    # Run the second daemon in a BOUNDED thread. On the main thread, a
    # regression that lets it bind would enter serve_forever and block
    # forever, hanging CI instead of reporting a failure.
    state: dict = {"exc": None, "daemon": None, "returned": False}

    def _start_second() -> None:
        try:
            second = RecallDaemon(
                socket_path=sock,
                retriever=_FakeRetriever(),
                refresh_interval_s=0.0,
            )
            state["daemon"] = second
            second.serve_forever()
            state["returned"] = True
        except BaseException as exc:  # noqa: BLE001 - asserted on below
            state["exc"] = exc

    thread = threading.Thread(target=_start_second, daemon=True, name="second-daemon")
    thread.start()
    thread.join(timeout=5.0)

    if thread.is_alive():
        # It bound and is serving: two owners of an exclusively locked store.
        # Stop it so the process doesn't leak a listener, then fail fast.
        rogue = state["daemon"]
        if rogue is not None:
            try:
                rogue.shutdown()
            except Exception:
                pass
        thread.join(timeout=5.0)
        pytest.fail(
            "a second daemon bound the socket and kept serving instead of "
            "refusing; both would then own the exclusively locked Qdrant store"
        )

    exc = state["exc"]
    assert exc is not None, (
        "the second daemon neither raised nor kept serving "
        f"(serve_forever returned={state['returned']}); startup must refuse "
        "when another daemon already answers on the socket"
    )
    assert isinstance(exc, (RuntimeError, OSError, SystemExit)), repr(exc)
    assert "already running" in str(exc).lower(), (
        f"the refusal must say the daemon is already running: {exc!r}"
    )

    # The refusal must not clobber the running daemon's socket.
    resp = _send(sock, {"v": 1, "op": "query", "prompt": "still alive", "k": 1})
    assert resp["ok"] is True, resp


def test_four_concurrent_clients_all_succeed(fake_daemon):
    """Connections are threaded: four hooks firing at once all get answers.

    Retrieval is serialized under one lock, so this pins that the SERVER
    doesn't serialize connections — a single-threaded accept loop would
    still pass a sequential test and stall a real burst.
    """
    _daemon, sock, _retriever = fake_daemon(
        retriever=_FakeRetriever(_results(2), delay_s=0.05)
    )

    responses: dict[int, object] = {}
    errors: dict[int, BaseException] = {}
    barrier = threading.Barrier(4)

    def _client(i: int) -> None:
        try:
            barrier.wait(timeout=5)
            responses[i] = _send(
                sock, {"v": 1, "op": "query", "prompt": f"prompt {i}", "k": 2}
            )
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors[i] = exc

    threads = [threading.Thread(target=_client, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, f"concurrent clients raised: {errors}"
    assert set(responses) == {0, 1, 2, 3}
    for i, resp in responses.items():
        assert resp is not None and resp["ok"] is True, f"client {i}: {resp}"
        assert len(resp["results"]) == 2


def test_max_request_bytes_is_bounded():
    """A single request can't be used to exhaust daemon memory."""
    assert isinstance(MAX_REQUEST_BYTES, int)
    assert 0 < MAX_REQUEST_BYTES <= 1 << 20


def _line_of_exactly(n_bytes: int, *, prompt: str = "atomic writes", k: int = 2) -> bytes:
    """A valid NDJSON request line of EXACTLY n_bytes, padded via session_id.

    Padding goes in `session_id`, not `prompt`: prompts over 20000 chars are
    their own documented `bad_request`, which would confound a size test.
    """
    base = {"v": 1, "op": "query", "prompt": prompt, "k": k, "session_id": ""}
    pad = n_bytes - len((json.dumps(base) + "\n").encode())
    assert pad >= 0, f"{n_bytes} is too small to hold a valid request"
    base["session_id"] = "s" * pad
    line = (json.dumps(base) + "\n").encode()
    assert len(line) == n_bytes
    return line


# How long a client waits before calling the server hung. Generous on
# purpose: the assertion under test is "the server does not park a thread
# forever", and the server's own bounds are 2 s (receive timeout) and an
# immediate size rejection. Anything under a few seconds would be measuring
# machine load — this suite runs with several other agents on the box —
# while 30 s still fails a genuine hang in well under the pytest timeout.
_HANG_TIMEOUT_S = 30.0


def _send_bounded(sock_path, raw: bytes, *, timeout: float):
    """Send raw bytes and require an answer OR a close inside `timeout`.

    A client-side timeout here means the SERVER hung, which is the failure
    this whole boundary exists to prevent: one oversized write from any local
    process would otherwise park a daemon thread indefinitely.
    """
    started = time.monotonic()
    try:
        resp = _send(sock_path, raw=raw, timeout=timeout)
    except (BrokenPipeError, ConnectionResetError):
        # The server rejected the stream and closed while we were still
        # writing. That IS the "closed connection" outcome, not a hang, and
        # it is the healthiest possible response to an over-cap request.
        return None, time.monotonic() - started
    except TimeoutError as exc:
        pytest.fail(
            f"server neither answered nor closed within {timeout}s "
            f"({len(raw)} byte request): {exc!r}"
        )
    except OSError as exc:
        pytest.fail(f"unexpected socket error on a {len(raw)} byte request: {exc!r}")
    return resp, time.monotonic() - started


def test_request_at_max_bytes_is_handled(fake_daemon):
    """A line of exactly MAX_REQUEST_BYTES is read to completion and answered.

    The boundary is inclusive. An off-by-one that rejects the limit itself
    would strand requests the protocol documents as legal.
    """
    _daemon, sock, _retriever = fake_daemon(results=_results(2))

    line = _line_of_exactly(MAX_REQUEST_BYTES)
    resp, elapsed = _send_bounded(sock, line, timeout=_HANG_TIMEOUT_S)

    assert resp is not None, (
        f"server closed on a legal {MAX_REQUEST_BYTES}-byte request without "
        f"answering (after {elapsed:.2f}s)"
    )
    assert resp["v"] == PROTOCOL_VERSION
    # SERVED, not merely answered. The request is valid in every other way:
    # well-formed JSON, known op, prompt well under the 20000-char limit. The
    # only thing under test is its length, and MAX_REQUEST_BYTES is the
    # largest LEGAL size. A `>=` where the code needs `>` rejects exactly this
    # request, and accepting a polite bad_request here would hide that.
    assert resp["ok"] is True, (
        f"a {MAX_REQUEST_BYTES}-byte request is at the limit, not over it, and "
        f"must be served; the cap is inclusive: {resp}"
    )
    assert len(resp["results"]) == 2


def test_oversized_request_is_rejected_or_closed_without_hanging(fake_daemon):
    """Over the cap, terminated or not, the server answers or closes fast.

    Two shapes, because they fail differently. A terminated over-cap line
    tests the size check. An UNTERMINATED oversized stream tests the 2 s
    receive timeout: without it, a client that writes forever and never sends
    a newline holds a daemon thread open until the process dies.

    Every bound here is `_HANG_TIMEOUT_S`, not a tight measurement of the
    server's own 2 s timeout. The failure being pinned is a HANG — a parked
    thread that never answers and never closes — and a tight wall-clock
    bound would instead be a load meter for whatever else is running on the
    machine.
    """
    _daemon, sock, _retriever = fake_daemon()

    # 1. One byte over the cap, properly newline-terminated.
    over = _line_of_exactly(MAX_REQUEST_BYTES + 1)
    resp, elapsed = _send_bounded(sock, over, timeout=_HANG_TIMEOUT_S)
    if resp is not None:
        assert resp["ok"] is False, (
            f"a {len(over)}-byte request exceeds MAX_REQUEST_BYTES and must "
            f"not be served: {resp}"
        )
        assert resp["error"] == "bad_request", resp
    assert elapsed < _HANG_TIMEOUT_S, (
        f"an over-cap request took {elapsed:.2f}s to be answered or closed"
    )

    # 2. Oversized and NEVER terminated: the receive timeout is the only
    #    thing that can end this connection.
    unterminated = b'{"v":1,"op":"query","prompt":"' + b"x" * (MAX_REQUEST_BYTES + 4096)
    assert b"\n" not in unterminated
    resp2, elapsed2 = _send_bounded(sock, unterminated, timeout=_HANG_TIMEOUT_S)
    if resp2 is not None:
        assert resp2["ok"] is False, resp2
        assert resp2["error"] == "bad_request", resp2
    assert elapsed2 < _HANG_TIMEOUT_S, (
        f"unterminated oversized stream took {elapsed2:.2f}s; the server "
        f"neither answered nor closed, so its receive timeout never fired"
    )

    # The daemon is still healthy afterwards — no leaked or wedged thread.
    healthy = _send(
        sock,
        {"v": 1, "op": "query", "prompt": "still alive", "k": 1},
        timeout=_HANG_TIMEOUT_S,
    )
    assert healthy["ok"] is True, healthy


# ---------------------------------------------------------------------------
# Index freshness (daemon-owned)
# ---------------------------------------------------------------------------


def test_refresh_once_sets_pending_then_clears_and_records_ok(
    fake_daemon, monkeypatch
):
    """A successful pass flips pending on, then records counts and clears it."""
    daemon, _sock, _retriever = fake_daemon(start=False)
    fake = _FakeRefresh(changed=2, deleted=1, ms=42, observer=daemon)
    monkeypatch.setattr("recall.daemon.refresh_index_chunked", fake)

    assert daemon.refresh_pending is False
    result = daemon.refresh_once()

    assert fake.calls == 1
    assert fake.observed_pending == [True], (
        "refresh_pending must be true WHILE the pass runs, so a query served "
        "mid-pass reports index_stale"
    )
    assert daemon.refresh_pending is False
    assert daemon.last_refresh_ok is True
    assert daemon.last_refresh_error is None
    assert daemon.last_refresh_changed == 2
    assert daemon.last_refresh_deleted == 1
    assert daemon.last_refresh_ms == 42
    assert isinstance(daemon.last_refresh_ts, float)
    assert daemon.index_stale() is False
    assert result["changed"] == 2
    assert result["deleted"] == 1


def test_refresh_failure_sets_last_refresh_error_and_index_stale(
    fake_daemon, monkeypatch
):
    """A failed pass is LOUD: the daemon keeps serving but says it's stale."""
    daemon, _sock, _retriever = fake_daemon(start=False)
    fake = _FakeRefresh(raises=RuntimeError("qdrant exploded"), observer=daemon)
    monkeypatch.setattr("recall.daemon.refresh_index_chunked", fake)

    daemon.refresh_once()

    assert daemon.last_refresh_ok is False
    assert "qdrant exploded" in (daemon.last_refresh_error or "")
    assert daemon.index_stale() is True, (
        "a failed refresh must mark the index stale; silently serving a stale "
        "brain is the failure mode this whole field exists to prevent"
    )


def test_status_reports_index_age_and_refresh_fields(fake_daemon, monkeypatch):
    """`index_age_s` is null until the first SUCCESSFUL pass, then a float."""
    daemon, sock, _retriever = fake_daemon()
    fake = _FakeRefresh(changed=1, deleted=0, ms=5, observer=daemon)
    monkeypatch.setattr("recall.daemon.refresh_index_chunked", fake)

    cold = _send(sock, {"v": 1, "op": "status"})
    assert cold["index_age_s"] is None
    assert cold["last_refresh_ok"] is None
    assert cold["refresh_interval_s"] == 0.0

    daemon.refresh_once()

    warm = _send(sock, {"v": 1, "op": "status"})
    assert isinstance(warm["index_age_s"], float)
    assert warm["index_age_s"] >= 0.0
    assert warm["last_refresh_ok"] is True
    assert warm["last_refresh_error"] is None
    assert warm["last_refresh_changed"] == 1
    assert warm["last_refresh_ms"] == 5
    assert warm["refresh_pending"] is False


def test_query_response_carries_index_stale(fake_daemon, monkeypatch):
    """The hook reads `index_stale` off the query response, not a second call."""
    daemon, sock, _retriever = fake_daemon()

    ok_pass = _FakeRefresh(changed=0, deleted=0, observer=daemon)
    monkeypatch.setattr("recall.daemon.refresh_index_chunked", ok_pass)
    daemon.refresh_once()
    fresh = _send(sock, {"v": 1, "op": "query", "prompt": "hello", "k": 1})
    assert fresh["index_stale"] is False, fresh

    bad_pass = _FakeRefresh(raises=RuntimeError("boom"), observer=daemon)
    monkeypatch.setattr("recall.daemon.refresh_index_chunked", bad_pass)
    daemon.refresh_once()
    stale = _send(sock, {"v": 1, "op": "query", "prompt": "hello", "k": 1})
    assert stale["index_stale"] is True, stale


def test_reindex_op_runs_refresh_once_and_returns_counts(fake_daemon, monkeypatch):
    """`recall reindex` routes here while the daemon owns the store."""
    daemon, sock, _retriever = fake_daemon()
    fake = _FakeRefresh(changed=3, deleted=2, ms=11, observer=daemon)
    monkeypatch.setattr("recall.daemon.refresh_index_chunked", fake)

    resp = _send(sock, {"v": 1, "op": "reindex"}, timeout=30.0)

    assert resp["ok"] is True, resp
    assert resp["changed"] == 3
    assert resp["deleted"] == 2
    assert isinstance(resp["ms"], int)
    assert fake.calls == 1


def test_refresh_interval_zero_disables_loop(fake_daemon, monkeypatch):
    """`--refresh-interval-s 0` means no background pass at all (tests only).

    Asserted on the THREAD, not on a sleep: "no pass ran within 300 ms" is a
    race dressed up as a test — under load the loop could simply not have
    got there yet. The daemon starts its refresh thread before it enters the
    accept loop, and the fixture does not return until a `status` request has
    round-tripped through that loop, so by the time this test runs the thread
    either exists or was never created.
    """
    fake = _FakeRefresh(changed=1)
    monkeypatch.setattr("recall.daemon.refresh_index_chunked", fake)

    _daemon, sock, _retriever = fake_daemon(refresh_interval_s=0.0)
    resp = _send(sock, {"v": 1, "op": "query", "prompt": "hello", "k": 1})
    assert resp["ok"] is True

    refresh_threads = [
        t.name for t in threading.enumerate() if t.name == "recall-daemon-refresh"
    ]
    assert not refresh_threads, (
        "refresh_interval_s=0 must not start the background refresh thread; "
        f"found {refresh_threads}"
    )
    assert fake.calls == 0, (
        "refresh_interval_s=0 must disable the background loop entirely; "
        f"it ran {fake.calls} pass(es)"
    )


# ---------------------------------------------------------------------------
# Contention: who is actually holding the lock, and for how long
#
# One lock serializes three different things — queries, the chunked upsert
# phase of a refresh, and the warm-up. What the daemon TELLS a client about a
# wait has to match which of them was in the way, and no wait may be
# unbounded. These tests drive both sides of the lock explicitly rather than
# sleeping and hoping.
# ---------------------------------------------------------------------------


def _run_query(daemon, out: dict, key: str, **kwargs) -> threading.Thread:
    """Fire one query on its own thread, recording the response in `out`."""

    def _go() -> None:
        try:
            out[key] = daemon.handle_request(_query_req(), **kwargs)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the caller
            out[key] = exc

    thread = threading.Thread(target=_go, daemon=True, name=f"query-{key}")
    thread.start()
    return thread


def test_query_queued_behind_another_query_is_not_blamed_on_a_refresh(
    fake_daemon, monkeypatch
):
    """A wait behind ANOTHER QUERY must never be reported as an index refresh.

    `refresh_index_chunked` runs its discovery and `stat()` phase completely
    outside the lock — seconds on a real brain — so "a refresh pass is in
    flight" says nothing about who holds the lock. Blaming the refresh there
    turns an ordinary queue into a `busy` the client cannot act on: retrying
    "once the refresh finishes" does not help, because the refresh was never
    in the way.
    """
    monkeypatch.setattr("recall.daemon.QUERY_LOCK_TIMEOUT_S", 0.2)
    retriever = _GatedRetriever()
    daemon, _sock, _r = fake_daemon(
        start=False, retriever=retriever, queue_timeout_s=30.0
    )
    refresh = _PhasedRefresh()
    monkeypatch.setattr("recall.daemon.refresh_index_chunked", refresh)

    pass_thread = threading.Thread(target=daemon.refresh_once, daemon=True)
    pass_thread.start()
    assert refresh.discovery_entered.wait(10), "the refresh pass never started"

    responses: dict = {}
    first = _run_query(daemon, responses, "first")
    assert retriever.entered.wait(10), "the first query never reached the retriever"

    # Second query, fired while the refresh is mid-pass but holding nothing.
    second = _run_query(daemon, responses, "second")
    # Let it wait well past QUERY_LOCK_TIMEOUT_S — the window in which the
    # daemon decides whether this wait is a refresh blockage or a queue.
    time.sleep(0.2 * 3)

    retriever.release.set()
    first.join(15)
    second.join(15)
    refresh.finish_discovery.set()
    assert refresh.lock_held.wait(10)
    refresh.release_lock.set()
    pass_thread.join(15)

    assert responses["first"]["ok"] is True, responses["first"]
    assert responses["second"]["ok"] is True, (
        "a query that queued behind another QUERY was rejected as busy while "
        "the refresh held nothing; the refresh flag must be set only inside "
        f"the pass's `with lock:` windows: {responses['second']}"
    )
    assert retriever.calls == 2


def test_query_blocked_by_the_refresh_chunk_phase_does_say_busy(
    fake_daemon, monkeypatch
):
    """The other half: when the refresh really DOES hold the lock, say so.

    The point of the honest flag is not to stop reporting `busy` — it is to
    report it only when true. Here the pass is inside its chunked-upsert
    window, so a client that retries shortly genuinely wins.
    """
    monkeypatch.setattr("recall.daemon.QUERY_LOCK_TIMEOUT_S", 0.2)
    daemon, _sock, _r = fake_daemon(start=False, queue_timeout_s=30.0)
    refresh = _PhasedRefresh()
    monkeypatch.setattr("recall.daemon.refresh_index_chunked", refresh)

    pass_thread = threading.Thread(target=daemon.refresh_once, daemon=True)
    pass_thread.start()
    assert refresh.discovery_entered.wait(10)
    refresh.finish_discovery.set()
    assert refresh.lock_held.wait(10), "the refresh never took the lock"

    started = time.monotonic()
    resp = daemon.handle_request(_query_req())
    elapsed = time.monotonic() - started

    refresh.release_lock.set()
    pass_thread.join(15)

    assert resp["ok"] is False and resp["error"] == "busy", resp
    assert "index refresh" in resp["message"], resp
    assert elapsed < 5.0, (
        f"a query blocked by a refresh chunk waited {elapsed:.2f}s before "
        f"being told; it must give up near QUERY_LOCK_TIMEOUT_S"
    )


def test_queued_query_gets_busy_at_the_bound_not_a_minute_later(
    fake_daemon, monkeypatch
):
    """The server-side queue wait is BOUNDED near the client's own budget.

    The hook gives up at 800 ms. A request thread that queues for a minute
    and then runs the query answers a socket nobody is reading, while
    holding the lock against clients that are still waiting — with `--rerank`
    a burst of four convoys into four wasted seconds.
    """
    monkeypatch.setattr("recall.daemon.QUERY_LOCK_TIMEOUT_S", 0.2)
    retriever = _GatedRetriever()
    daemon, _sock, _r = fake_daemon(
        start=False, retriever=retriever, queue_timeout_s=0.3
    )

    responses: dict = {}
    first = _run_query(daemon, responses, "first")
    assert retriever.entered.wait(10)

    started = time.monotonic()
    resp = daemon.handle_request(_query_req())
    elapsed = time.monotonic() - started

    retriever.release.set()
    first.join(15)

    assert resp["ok"] is False and resp["error"] == "busy", resp
    assert "index refresh" not in resp["message"], (
        f"no refresh was running; the message must not invent one: {resp}"
    )
    assert elapsed < 5.0, (
        f"the queued request waited {elapsed:.2f}s; with a 0.3 s bound it "
        f"must be told busy promptly, not queue for QUERY_QUEUE_TIMEOUT_S"
    )
    assert retriever.calls == 1, (
        "the rejected query must not also have run: it was rejected because "
        "running it would be wasted work"
    )


def test_queue_bound_defaults_near_the_hook_budget_and_is_configurable(
    short_sock_dir, monkeypatch
):
    """2 s default (~2x the hook's 800 ms), overridable per daemon and by env.

    A client that will genuinely wait longer says so with `budget_ms`, and
    the daemon queues for up to 2x that — capped, so no request thread can
    be parked indefinitely by a client's claim.
    """
    from recall.daemon import QUERY_QUEUE_TIMEOUT_MAX_S, QUERY_QUEUE_TIMEOUT_S

    assert 0 < QUERY_QUEUE_TIMEOUT_S <= 5.0, (
        f"the default server-side queue wait is {QUERY_QUEUE_TIMEOUT_S}s; it "
        f"has to stay near the hook's 800 ms budget"
    )

    sock = short_sock_dir / "cfg.sock"
    default = RecallDaemon(socket_path=sock, retriever=_FakeRetriever())
    assert default.queue_timeout_s == QUERY_QUEUE_TIMEOUT_S

    monkeypatch.setenv("RECALL_DAEMON_QUEUE_TIMEOUT_S", "7.5")
    assert RecallDaemon(socket_path=sock, retriever=_FakeRetriever()).queue_timeout_s == 7.5
    monkeypatch.setenv("RECALL_DAEMON_QUEUE_TIMEOUT_S", "not-a-number")
    assert (
        RecallDaemon(socket_path=sock, retriever=_FakeRetriever()).queue_timeout_s
        == QUERY_QUEUE_TIMEOUT_S
    ), "an unparseable override must fall back, not stop the daemon starting"
    monkeypatch.delenv("RECALL_DAEMON_QUEUE_TIMEOUT_S")

    explicit = RecallDaemon(
        socket_path=sock, retriever=_FakeRetriever(), queue_timeout_s=0.5
    )
    assert explicit.queue_timeout_s == 0.5
    assert explicit._queue_timeout_for({}) == 0.5
    assert explicit._queue_timeout_for({"budget_ms": 60000}) == min(
        120.0, QUERY_QUEUE_TIMEOUT_MAX_S
    )
    assert explicit._queue_timeout_for({"budget_ms": 100}) == 0.5
    assert explicit._queue_timeout_for({"budget_ms": True}) == 0.5


def test_queued_query_is_dropped_when_the_client_hangs_up(fake_daemon, monkeypatch):
    """A client that closed gets nothing run on its behalf.

    The hook's budget is 800 ms; after that it has already fallen back and
    printed. Running its query anyway costs the lock, the embedder and (with
    `--rerank`) the cross-encoder, for a result that goes nowhere.
    """
    monkeypatch.setattr("recall.daemon.QUERY_LOCK_TIMEOUT_S", 0.2)
    retriever = _GatedRetriever()
    daemon, _sock, _r = fake_daemon(
        start=False, retriever=retriever, queue_timeout_s=30.0
    )

    responses: dict = {}
    first = _run_query(daemon, responses, "first")
    assert retriever.entered.wait(10)

    started = time.monotonic()
    resp = daemon.handle_request(_query_req(), is_connected=lambda: False)
    elapsed = time.monotonic() - started

    retriever.release.set()
    first.join(15)

    assert resp["ok"] is False and resp["error"] == "busy", resp
    assert "closed" in resp["message"], resp
    assert elapsed < 5.0, (
        f"a disconnected client's request queued for {elapsed:.2f}s of its "
        f"30 s bound instead of being dropped"
    )
    assert retriever.calls == 1, "the dropped query must never reach the retriever"


def test_reindex_joins_a_running_refresh_instead_of_double_embedding(
    fake_daemon, monkeypatch
):
    """Refresh passes are mutually exclusive, and `reindex` joins the running one.

    Two concurrent passes (the background loop plus a `reindex` op, or
    `recall reindex` twice) walk and embed the whole brain twice — and the
    one that finishes first clears `refresh_pending` while the other is
    still upserting, so `index_stale()` reports False in the middle of a
    refresh and every `x_index_stale` in that window is wrong.
    """
    daemon, _sock, _r = fake_daemon(start=False)
    refresh = _PhasedRefresh(changed=2, deleted=1, ms=5)
    monkeypatch.setattr("recall.daemon.refresh_index_chunked", refresh)

    background = threading.Thread(target=daemon.refresh_once, daemon=True)
    background.start()
    assert refresh.discovery_entered.wait(10), "the background pass never started"
    assert daemon.index_stale() is True
    assert daemon.refresh_in_flight() is True

    op: dict = {}

    def _reindex() -> None:
        op["resp"] = daemon.handle_request({"v": 1, "op": "reindex"})

    op_thread = threading.Thread(target=_reindex, daemon=True, name="reindex-op")
    op_thread.start()

    assert not refresh.second_call.wait(1.0), (
        "the reindex op started a SECOND concurrent refresh pass; both would "
        "walk and embed the whole brain, and the first to finish would clear "
        "refresh_pending underneath the other"
    )
    assert daemon.index_stale() is True, (
        "refresh_pending was cleared while the first pass was still running"
    )

    refresh.finish_discovery.set()
    assert refresh.lock_held.wait(10)
    refresh.release_lock.set()
    background.join(15)
    op_thread.join(15)

    assert refresh.calls == 1, (
        f"one refresh pass must have run, not {refresh.calls}"
    )
    assert op["resp"]["ok"] is True, op["resp"]
    assert op["resp"]["changed"] == 2, (
        f"the reindex op must return the running pass's result: {op['resp']}"
    )
    assert op["resp"]["deleted"] == 1, op["resp"]
    assert daemon.refresh_pending is False
    assert daemon.index_stale() is False
    assert daemon.refresh_in_flight() is False


# ---------------------------------------------------------------------------
# Startup: permissions, error visibility, and the two-owners case
# ---------------------------------------------------------------------------


def test_socket_is_never_world_connectable_between_bind_and_chmod(
    fake_daemon, monkeypatch
):
    """`bind()` itself must create a private socket, not chmod one afterwards.

    `bind()` applies the process umask. With the usual 0022 that is a
    world-CONNECTABLE socket for the window between bind and chmod, and
    `~/.agent/runtime` is typically 0755, so the path is reachable: any
    local user who wins that race can query the brain. The umask is restored
    afterwards because the daemon writes Qdrant storage with it.
    """
    entry_umask = os.umask(0o000)  # worst case: nothing masked by default
    try:
        observed: dict = {}
        real_chmod = os.chmod

        def _recording_chmod(path, mode, *args, **kwargs):
            try:
                observed.setdefault("pre_chmod", stat.S_IMODE(os.stat(path).st_mode))
            except OSError:  # pragma: no cover - not a path we created
                pass
            return real_chmod(path, mode, *args, **kwargs)

        monkeypatch.setattr(os, "chmod", _recording_chmod)
        _daemon, sock, _retriever = fake_daemon()

        # AF_UNIX sockets are created 0777 & ~umask, so under 0o077 the
        # pre-chmod mode is 0700 — owner-only, which is the whole point.
        # What must be zero are the group and other bits; the chmod that
        # follows only strips the (harmless) execute bit.
        pre_chmod = observed.get("pre_chmod")
        assert pre_chmod is not None, "the daemon never chmod'ed its socket"
        assert pre_chmod & 0o077 == 0, (
            f"the socket existed with mode {oct(pre_chmod)} before chmod ran, "
            f"i.e. connectable by other local users; bind must happen under a "
            f"0o077 umask so that window never exists"
        )
        assert stat.S_IMODE(os.stat(sock).st_mode) == 0o600

        restored = os.umask(0o000)
        assert restored == 0o000, (
            f"the daemon left the process umask at {oct(restored)}; it must "
            f"restore what it found (Qdrant storage is written with it)"
        )
    finally:
        os.umask(entry_umask)


def test_handle_error_logs_once_per_exception_type(fake_daemon, capsys):
    """A persistent crash outside `handle_request` must not be invisible.

    Swallowing everything keeps one bad connection from killing the daemon,
    but it also means a systematic failure in `handle()` shows up nowhere in
    the launchd log. One line per exception TYPE: diagnosable, and still
    bounded if every single connection fails.
    """
    daemon, _sock, _retriever = fake_daemon()
    server = daemon._server
    assert server is not None

    for _ in range(3):
        try:
            raise ValueError("connection went sideways")
        except ValueError:
            server.handle_error(None, None)
    try:
        raise KeyError("something else")
    except KeyError:
        server.handle_error(None, None)

    out = capsys.readouterr().out
    logged = [line for line in out.splitlines() if "connection handler raised" in line]
    assert sum("ValueError" in line for line in logged) == 1, (
        f"one line per exception type, not per occurrence:\n{out}"
    )
    assert "connection went sideways" in out, out
    assert sum("KeyError" in line for line in logged) == 1, (
        f"a different exception type must also log:\n{out}"
    )


def test_second_daemon_backs_off_before_exiting_and_names_the_pid(
    fake_daemon, monkeypatch, capsys
):
    """Under launchd `KeepAlive`, exit 1 is a respawn every ThrottleInterval.

    When a manual `recall serve` owns the socket, a bare exit turns into the
    same refusal six times a minute in recall-daemon.stderr.log forever.
    Sleeping first keeps the message without the spam, and naming the pid
    tells the user what to kill.
    """
    import signal as _signal

    from recall.daemon import run_daemon

    _daemon, sock, _retriever = fake_daemon()
    monkeypatch.setenv("RECALL_DAEMON_BUSY_BACKOFF_S", "0.4")

    handlers = {
        sig: _signal.getsignal(sig) for sig in (_signal.SIGTERM, _signal.SIGINT)
    }
    started = time.monotonic()
    try:
        rc = run_daemon(socket_path=sock, refresh_interval_s=0.0)
    finally:
        for sig, handler in handlers.items():
            _signal.signal(sig, handler)
    elapsed = time.monotonic() - started

    assert rc == 1, "a daemon that cannot own the socket must exit non-zero"
    assert elapsed >= 0.35, (
        f"exited after {elapsed:.2f}s without backing off; launchd would "
        f"respawn straight into the same refusal"
    )
    err = capsys.readouterr().err
    assert f"pid {os.getpid()}" in err, (
        f"the refusal must name the process holding the socket:\n{err}"
    )
    assert "already running" in err.lower(), err


# ---------------------------------------------------------------------------
# What `status` reports about the process that is actually running
# ---------------------------------------------------------------------------


def test_model_info_reads_the_running_retrievers_models(fake_daemon):
    """`status` reports what is LOADED, read off `HybridRetriever`'s real
    attributes (`_dense_model`, `_reranker`, `_reranker_model`,
    `_rerank_n` — see recall/core.py). A health check compares this against
    the calibrated model, so probing an attribute the retriever never had
    and falling back to the config would report what was asked for rather
    than what is running."""

    class _ModelledRetriever(_FakeRetriever):
        _dense_model = "BAAI/bge-small-en-v1.5"
        _sparse_model = "Qdrant/bm25"
        _reranker = "cross_encoder"
        _reranker_model = "Xenova/ms-marco-MiniLM-L-6-v2"
        _rerank_n = 10

    _daemon, sock, _retriever = fake_daemon(retriever=_ModelledRetriever())

    resp = _send(sock, {"v": 1, "op": "status"})

    assert resp["embedder"] == "BAAI/bge-small-en-v1.5", (
        f"status must report the retriever's loaded dense model: {resp}"
    )
    assert resp["reranker_model"] == "Xenova/ms-marco-MiniLM-L-6-v2", resp
    assert resp["rerank_n"] == 10, resp

    query = _send(sock, {"v": 1, "op": "query", "prompt": "hello", "k": 1})
    assert query["model"]["embedder"] == "BAAI/bge-small-en-v1.5", query
    assert query["model"]["reranker"] == "cross_encoder", query


def test_status_mode_reports_the_effective_mode(short_sock_dir, monkeypatch):
    """`RECALL_MODE` beats the config file everywhere else (`effective_mode`),
    including in the retriever this daemon built. Reporting `cfg.ranking.mode`
    would tell a health check the daemon is running hybrid retrieval when it
    is running sparse."""
    from recall.config import Config, RankingConfig, SourceConfig

    cfg = Config(
        sources=[
            SourceConfig(
                name="brain",
                path="/nonexistent",
                glob="**/*.md",
                frontmatter="optional",
                exclude=[],
            )
        ],
        ranking=RankingConfig(mode="hybrid"),
    )
    monkeypatch.setenv("RECALL_MODE", "sparse")

    daemon = RecallDaemon(
        socket_path=short_sock_dir / "mode.sock",
        cfg=cfg,
        retriever=_FakeRetriever(),
        refresh_interval_s=0.0,
    )

    assert daemon.status()["mode"] == "sparse", (
        "status must report the mode the retriever actually runs with "
        "($RECALL_MODE > config), not the raw config value"
    )
    assert daemon.status()["collections"] == ["brain"]


# ---------------------------------------------------------------------------
# The other half of the freshness contract: the in-process fallback
# ---------------------------------------------------------------------------


class TestInProcessFallbackNeverRefreshes:
    """CROSS-SLICE PIN. The code under test (`auto_recall._load_retriever`) is
    slice B's file; the guarantee it has to uphold is slice C's.

    Freshness moved to the daemon, which means the hook's fallback path must
    NOT refresh. `needs_refresh` walks every source and stats every file, and
    `build_index` embeds; either one blows through the hook's 1500 ms budget,
    so the prompt gets nothing injected AND pays the full stall. The fallback
    is allowed to serve a stale index — whatever the daemon or the last CLI
    query left behind — and `x_index_stale` is deliberately absent on this
    path because staleness is unknown here rather than false.

    Lives here rather than in `tests/runtime/test_hook_daemon_path.py`
    because slice B owns that file. Move it there at merge if preferred.
    """

    def test_inproc_loader_never_refreshes_index(
        self, isolated_xdg, write_config, empty_brain, monkeypatch
    ):
        import recall.core as core_mod
        import recall.index as index_mod
        from runtime.adapters.claude_code import auto_recall as ar_mod

        write_config(
            sources=[
                {
                    "name": "brain",
                    "path": str(empty_brain),
                    "glob": "**/*.md",
                    "frontmatter": "optional",
                    "exclude": [],
                }
            ]
        )

        def _boom(*args, **kwargs):
            raise AssertionError(
                "the in-process hook fallback refreshed the index; that walk "
                "or embed cannot fit inside the auto-recall timeout"
            )

        monkeypatch.setattr(index_mod, "needs_refresh", _boom)
        monkeypatch.setattr(index_mod, "build_index", _boom)
        # Slice C's chunked pass is daemon-only too; raising=False so this
        # holds both before and after recall/index.py grows the function.
        monkeypatch.setattr(
            index_mod, "refresh_index_chunked", _boom, raising=False
        )

        captured: dict = {}

        class _StubRetriever:
            def __init__(self, documents=None, **kwargs):
                captured["documents"] = documents
                captured["kwargs"] = kwargs

            def query(self, *args, **kwargs):
                return []

        monkeypatch.setattr(core_mod, "HybridRetriever", _StubRetriever)

        retriever = ar_mod._load_retriever()

        assert isinstance(retriever, _StubRetriever)
        assert captured["documents"] is None, (
            "the fallback must use the cold-start pattern (documents=None); "
            "passing documents re-walks and re-embeds the brain"
        )
        assert "collections" in captured["kwargs"], captured["kwargs"]


# ---------------------------------------------------------------------------
# Real brain (heavy, excluded from make test-ci)
# ---------------------------------------------------------------------------


def _write_brain_config(write_config, brain: Path) -> None:
    write_config(
        sources=[
            {
                "name": "brain",
                "path": str(brain),
                "glob": "**/*.md",
                "frontmatter": "auto-memory",
                "exclude": [],
            }
        ]
    )


def _reindex() -> None:
    """Build the real index through the CLI, which closes the Qdrant client
    in its `finally` so the daemon can take exclusive ownership after."""
    from typer.testing import CliRunner

    from recall.cli import app

    result = CliRunner().invoke(app, ["reindex"])
    assert result.exit_code == 0, result.output


@pytest.mark.embeddings
def test_real_brain_round_trip_has_rerank_scores(
    isolated_xdg, write_config, auto_memory_brain, fake_daemon
):
    """Against a real index with the reranker on, every result carries a
    `rerank_score`. That field is what the S4 relevance gate thresholds on;
    if it comes back null the gate silently degrades to RRF-only."""
    _write_brain_config(write_config, auto_memory_brain)
    _reindex()

    _daemon, sock, _retriever = fake_daemon(retriever=None, rerank=True)

    resp = _send(sock, {"v": 1, "op": "query", "prompt": "atomic writes", "k": 3},
                 timeout=120.0)

    assert resp["ok"] is True, resp
    assert resp["reranked"] is True, resp
    assert resp["results"], "real brain returned no results for 'atomic writes'"
    assert set(resp["results"][0]) == WIRE_KEYS
    assert all(r["rerank_score"] is not None for r in resp["results"]), resp
    assert all(isinstance(r["score"], float) for r in resp["results"])


@pytest.mark.embeddings
def test_four_concurrent_real_queries(
    isolated_xdg, write_config, auto_memory_brain, fake_daemon
):
    """Four real reranked queries at once all complete. Embedded Qdrant is
    thread-hostile unless every client call goes through the daemon's single
    lock; this is the test that catches losing that."""
    _write_brain_config(write_config, auto_memory_brain)
    _reindex()

    _daemon, sock, _retriever = fake_daemon(retriever=None, rerank=True)
    # Warm the models once so the four clients race retrieval, not downloads.
    _send(sock, {"v": 1, "op": "query", "prompt": "warm", "k": 1}, timeout=300.0)

    prompts = ["atomic writes", "pin dependencies", "unicode filenames", "release freeze"]
    responses: dict[int, object] = {}
    errors: dict[int, BaseException] = {}
    barrier = threading.Barrier(len(prompts))

    def _client(i: int) -> None:
        try:
            barrier.wait(timeout=10)
            responses[i] = _send(
                sock, {"v": 1, "op": "query", "prompt": prompts[i], "k": 3},
                timeout=120.0,
            )
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors[i] = exc

    threads = [threading.Thread(target=_client, args=(i,)) for i in range(len(prompts))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=180)

    assert not errors, f"concurrent real queries raised: {errors}"
    assert set(responses) == set(range(len(prompts)))
    for i, resp in responses.items():
        assert resp is not None and resp["ok"] is True, f"client {i}: {resp}"
        assert resp["results"], f"client {i} ({prompts[i]}) got no results"


@pytest.mark.embeddings
def test_new_file_is_queryable_after_refresh_once(
    isolated_xdg, write_config, auto_memory_brain, fake_daemon
):
    """A memory written after startup is retrievable once a pass runs.

    This is the whole point of daemon-owned freshness: the CLI no longer
    refreshes, so a doc the dream cycle just wrote must land via refresh_once.
    """
    _write_brain_config(write_config, auto_memory_brain)
    _reindex()

    _daemon, sock, _retriever = fake_daemon(retriever=None, rerank=True)

    new_doc = auto_memory_brain / "semantic" / "lessons" / "feedback_kestrel_latch.md"
    new_doc.write_text(
        "---\n"
        "name: kestrel-latch\n"
        "type: feedback\n"
        "description: the kestrel latch must be closed before ballast transfer\n"
        "---\n"
        "Always close the kestrel latch before transferring ballast.\n",
        encoding="utf-8",
    )

    result = _daemon.refresh_once()
    assert result["changed"] >= 1, result

    resp = _send(
        sock, {"v": 1, "op": "query", "prompt": "kestrel latch ballast", "k": 5},
        timeout=120.0,
    )
    assert resp["ok"] is True, resp
    assert resp["index_stale"] is False, resp
    paths = [r["path"] for r in resp["results"]]
    assert str(new_doc) in paths, (
        f"a file written after startup was not queryable after refresh_once: {paths}"
    )
