"""Thin daemon client: every failure mode maps to a named reason.

`recall.daemon_client` is stdlib-only on purpose — it runs inside the Claude
Code UserPromptSubmit hook on every prompt, and importing `recall.core` or
`qdrant_client` there would cost hundreds of milliseconds before any work
starts. These tests pin that surface without a real daemon.

The reason strings are not cosmetic. The hook branches on them:

  * `no_socket` / `connection_refused` — the daemon is DOWN, so falling back
    to the in-process retriever is safe.
  * `timeout` / `protocol_error` / `server_error` — the daemon is UP and holds
    the exclusive Qdrant lock, so an in-process fallback would just block on
    fcntl until the hook's own timeout kills it. The hook reports
    `unavailable` instead.

Get the mapping wrong and the failure is invisible: every prompt silently
pays a 1.5 s stall and injects nothing.

Fake servers bind under a short `/tmp` dir because AF_UNIX paths are capped
at 104 bytes on macOS and pytest's `tmp_path` exceeds that.
"""

from __future__ import annotations

import json
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import pytest

from recall.daemon_client import DaemonUnavailable, query, request, status


@pytest.fixture
def short_sock_dir():
    d = tempfile.mkdtemp(prefix="rsd-", dir="/tmp")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def fake_server(short_sock_dir):
    """Bind a listening AF_UNIX socket driven by a per-connection handler.

    The handler receives the raw request bytes and returns the raw bytes to
    write back; returning None means "accept and never answer" (the timeout
    case). Sockets are closed on teardown.
    """
    servers: list[socket.socket] = []
    stop = threading.Event()

    def _serve(handler: Optional[Callable[[bytes], Optional[bytes]]] = None,
               name: str = "recall.sock") -> Path:
        path = short_sock_dir / name
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(path))
        srv.listen(8)
        srv.settimeout(0.2)
        servers.append(srv)

        def _loop():
            while not stop.is_set():
                try:
                    conn, _ = srv.accept()
                except (TimeoutError, OSError):
                    continue
                with conn:
                    try:
                        conn.settimeout(2.0)
                        try:
                            data = conn.recv(65536)
                        except (TimeoutError, OSError):
                            data = b""
                        reply = handler(data) if handler is not None else None
                        if reply is None:
                            # Hold the connection open without answering.
                            stop.wait(3.0)
                        else:
                            conn.sendall(reply)
                    except OSError:
                        pass

        threading.Thread(target=_loop, daemon=True, name="fake-daemon").start()
        return path

    yield _serve

    stop.set()
    for srv in servers:
        try:
            srv.close()
        except OSError:
            pass


def _json_line(payload: dict) -> bytes:
    return (json.dumps(payload) + "\n").encode()


def test_no_socket_reason(short_sock_dir):
    """Nothing bound at the path: the daemon was never installed."""
    missing = short_sock_dir / "not-there.sock"
    assert not missing.exists()

    with pytest.raises(DaemonUnavailable) as excinfo:
        request(missing, {"v": 1, "op": "status"}, budget_ms=200)

    assert excinfo.value.reason == "no_socket", excinfo.value.reason


def test_connection_refused_reason(short_sock_dir):
    """A socket file exists but nobody is listening: the daemon died.

    Bound-without-listen is the reliable way to get ECONNREFUSED on an
    AF_UNIX path on both macOS and Linux.
    """
    path = short_sock_dir / "bound-not-listening.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))  # deliberately NO listen()
    try:
        assert path.exists()

        with pytest.raises(DaemonUnavailable) as excinfo:
            request(path, {"v": 1, "op": "status"}, budget_ms=200)

        assert excinfo.value.reason == "connection_refused", excinfo.value.reason
    finally:
        srv.close()


def test_timeout_reason(fake_server):
    """Server accepts and never replies: the client gives up on budget.

    Budget is 200 ms; the assertion on elapsed time is what catches a client
    that ignores its own deadline and blocks on the socket default instead.
    """
    path = fake_server(handler=None)

    started = time.monotonic()
    with pytest.raises(DaemonUnavailable) as excinfo:
        request(path, {"v": 1, "op": "status"}, budget_ms=200)
    elapsed_ms = (time.monotonic() - started) * 1000

    assert excinfo.value.reason == "timeout", excinfo.value.reason
    assert elapsed_ms < 2000, (
        f"client blew past its 200 ms budget ({elapsed_ms:.0f} ms) — the hook "
        f"would stall on every prompt"
    )


def test_protocol_error_on_garbage_line(fake_server):
    """A reply that isn't JSON means the thing on the socket isn't our daemon."""
    path = fake_server(handler=lambda _data: b"<html>not a daemon</html>\n")

    with pytest.raises(DaemonUnavailable) as excinfo:
        request(path, {"v": 1, "op": "status"}, budget_ms=500)

    assert excinfo.value.reason == "protocol_error", excinfo.value.reason


def test_server_error_maps_ok_false(fake_server):
    """`ok:false` is an error, not a result. Raise instead of returning it."""
    path = fake_server(
        handler=lambda _data: _json_line(
            {"v": 1, "ok": False, "error": "internal", "message": "retrieval blew up"}
        )
    )

    with pytest.raises(DaemonUnavailable) as excinfo:
        request(path, {"v": 1, "op": "query", "prompt": "hi", "k": 1}, budget_ms=500)

    assert excinfo.value.reason == "server_error", excinfo.value.reason
    assert "retrieval blew up" in str(excinfo.value)


def test_status_none_when_absent(short_sock_dir):
    """`status()` answers None rather than raising: callers use it as a probe.

    `recall doctor` and `recall serve --status` both branch on None; making
    them catch an exception instead would be a worse interface.
    """
    assert status(short_sock_dir / "nope.sock", timeout_s=0.2) is None


def test_query_happy_path_returns_payload(fake_server):
    """The success path forwards the op and hands back the parsed response."""
    seen: list[dict] = []

    def _handler(data: bytes) -> bytes:
        seen.append(json.loads(data.decode().splitlines()[0]))
        return _json_line(
            {
                "v": 1,
                "ok": True,
                "results": [{"path": "/brain/a.md", "score": 0.5}],
                "query_ms": 12,
                "degraded": False,
                "reranked": True,
                "index_stale": False,
                "model": {"embedder": "bge", "reranker": "jina"},
            }
        )

    path = fake_server(handler=_handler)

    resp = query("atomic writes", k=3, socket_path=path, budget_ms=1000)

    assert resp["ok"] is True
    assert resp["results"][0]["path"] == "/brain/a.md"
    assert seen and seen[0]["op"] == "query"
    assert seen[0]["v"] == 1
    assert seen[0]["prompt"] == "atomic writes"
    assert seen[0]["k"] == 3
    # the server bounds its queue wait by ~2x this hint, so it must be forwarded
    assert seen[0]["budget_ms"] == 1000


def test_daemon_down_reasons_is_the_shared_fallback_contract():
    """Every daemon client (hook, CLI, MCP, the daemon's own stale-socket
    probe) decides "safe to fall back in-process?" from the SAME set. A
    caller that hard-codes its own copy drifts the moment a reason is added."""
    from recall.daemon_client import DAEMON_DOWN_REASONS

    assert DAEMON_DOWN_REASONS == frozenset({"no_socket", "connection_refused"})
    assert "timeout" not in DAEMON_DOWN_REASONS
    assert "server_error" not in DAEMON_DOWN_REASONS
    assert "protocol_error" not in DAEMON_DOWN_REASONS
