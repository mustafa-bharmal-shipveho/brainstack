"""MCP handler <-> daemon seam: fall back in-process only when the daemon is DOWN.

`recall_query_handler` tries the warm daemon first. The daemon holds the
embedded store's exclusive lock for as long as it runs, so the in-process
fallback is only safe when nothing is listening (`no_socket`,
`connection_refused`). A daemon that is UP but did not answer — `timeout`,
`protocol_error`, `server_error` — must surface as an error the MCP client
can retry, not as a blocking fallback that ends in "index is busy".
(Codex review, pass 3.)

Pure-Python handler, so these tests do not need the `mcp` extra. Fake
servers bind under a short /tmp dir: AF_UNIX paths are capped at 104 bytes
on macOS and pytest's tmp_path exceeds that.
"""

from __future__ import annotations

import json
import shutil
import socket
import tempfile
import threading
from pathlib import Path
from typing import Callable, Optional

import pytest

from recall import mcp_server


@pytest.fixture
def short_sock_dir():
    d = tempfile.mkdtemp(prefix="rsm-", dir="/tmp")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def fake_server(short_sock_dir):
    """Bind a listening AF_UNIX socket; `handler(raw) -> reply bytes | None`
    (None = accept and never answer, the timeout case)."""
    servers: list[socket.socket] = []
    stop = threading.Event()

    def _serve(handler: Optional[Callable[[bytes], Optional[bytes]]]) -> Path:
        path = short_sock_dir / "recall.sock"
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
                            stop.wait(3.0)
                        else:
                            conn.sendall(reply)
                    except OSError:
                        pass

        threading.Thread(target=_loop, daemon=True, name="fake-mcp-daemon").start()
        return path

    yield _serve

    stop.set()
    for srv in servers:
        try:
            srv.close()
        except OSError:
            pass


@pytest.fixture
def in_process_probe(monkeypatch):
    """Make the in-process path observable. It records that it ran and
    returns an empty index so the handler answers `[]` without a store."""
    seen = {"fell_back": False}

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

    def _load_config():
        seen["fell_back"] = True
        return _Cfg()

    monkeypatch.setattr(mcp_server, "load_config", _load_config)
    monkeypatch.setattr(mcp_server, "needs_refresh", lambda sources: False)
    monkeypatch.setattr(mcp_server, "load_index", lambda sources: None)
    monkeypatch.delenv("RECALL_NO_DAEMON", raising=False)
    # Keep the "daemon accepted but never answered" case fast.
    monkeypatch.setattr(mcp_server, "_DAEMON_BUDGET_MS", 300)
    return seen


def _json_line(payload: dict) -> bytes:
    return (json.dumps(payload) + "\n").encode()


# --- daemon DOWN: fall back --------------------------------------------------


def test_no_socket_falls_back_in_process(short_sock_dir, in_process_probe, monkeypatch):
    monkeypatch.setenv("RECALL_DAEMON_SOCKET", str(short_sock_dir / "missing.sock"))

    assert mcp_server.recall_query_handler("atomic writes", k=3) == []
    assert in_process_probe["fell_back"] is True


def test_connection_refused_falls_back_in_process(
    short_sock_dir, in_process_probe, monkeypatch
):
    path = short_sock_dir / "dead.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))  # bound, never listen(): the daemon died
    try:
        monkeypatch.setenv("RECALL_DAEMON_SOCKET", str(path))
        assert mcp_server.recall_query_handler("atomic writes", k=3) == []
        assert in_process_probe["fell_back"] is True
    finally:
        srv.close()


# --- daemon UP but unusable: surface, never fall back -----------------------


def test_server_error_is_surfaced_not_swallowed(fake_server, in_process_probe, monkeypatch):
    path = fake_server(
        lambda _raw: _json_line(
            {"v": 1, "ok": False, "error": "busy",
             "message": "retrieval is serialized and another query still holds it"}
        )
    )
    monkeypatch.setenv("RECALL_DAEMON_SOCKET", str(path))

    with pytest.raises(RuntimeError, match=r"daemon.*server_error") as excinfo:
        mcp_server.recall_query_handler("atomic writes", k=3)

    assert "still holds it" in str(excinfo.value), "the daemon's own message must survive"
    assert in_process_probe["fell_back"] is False, (
        "fell back into the store the live daemon holds the lock on"
    )


def test_timeout_is_surfaced_not_swallowed(fake_server, in_process_probe, monkeypatch):
    path = fake_server(None)  # accepts, never answers
    monkeypatch.setenv("RECALL_DAEMON_SOCKET", str(path))

    with pytest.raises(RuntimeError, match=r"daemon.*timeout"):
        mcp_server.recall_query_handler("atomic writes", k=3)

    assert in_process_probe["fell_back"] is False


def test_protocol_error_is_surfaced_not_swallowed(
    fake_server, in_process_probe, monkeypatch
):
    path = fake_server(lambda _raw: b"<html>not a daemon</html>\n")
    monkeypatch.setenv("RECALL_DAEMON_SOCKET", str(path))

    with pytest.raises(RuntimeError, match=r"daemon.*protocol_error"):
        mcp_server.recall_query_handler("atomic writes", k=3)

    assert in_process_probe["fell_back"] is False


# --- daemon UP and healthy: its answer IS the answer ------------------------


def test_daemon_results_are_returned_in_cli_json_shape(
    fake_server, in_process_probe, monkeypatch
):
    seen: list[dict] = []

    def _handler(raw: bytes) -> bytes:
        seen.append(json.loads(raw.decode().splitlines()[0]))
        return _json_line(
            {
                "v": 1, "ok": True, "query_ms": 12, "degraded": False,
                "reranked": False, "index_stale": False,
                "results": [
                    {"path": "/brain/a.md", "source": "brain", "title": "A",
                     "name": "a", "type": "project", "description": "d",
                     "score": 0.5, "rerank_score": None, "provenance": None},
                ],
            }
        )

    path = fake_server(_handler)
    monkeypatch.setenv("RECALL_DAEMON_SOCKET", str(path))

    out = mcp_server.recall_query_handler("atomic writes", k=3, source="brain", type="project")

    assert in_process_probe["fell_back"] is False
    assert seen and seen[0]["op"] == "query" and seen[0]["k"] == 3
    # Wire keys are `source`/`type` (the CLI's kwargs are *_filter).
    assert seen[0]["source"] == "brain" and seen[0]["type"] == "project"
    assert out == [
        {"path": "/brain/a.md", "source": "brain", "name": "a", "type": "project",
         "description": "d", "score": 0.5, "rerank_score": None, "provenance": None}
    ]
    json.dumps(out)


def test_recall_no_daemon_env_skips_the_socket_entirely(
    fake_server, in_process_probe, monkeypatch
):
    calls: list[bytes] = []
    path = fake_server(lambda raw: calls.append(raw) or _json_line({"v": 1, "ok": True, "results": []}))
    monkeypatch.setenv("RECALL_DAEMON_SOCKET", str(path))
    monkeypatch.setenv("RECALL_NO_DAEMON", "1")

    assert mcp_server.recall_query_handler("atomic writes", k=3) == []
    assert in_process_probe["fell_back"] is True
    assert calls == []
