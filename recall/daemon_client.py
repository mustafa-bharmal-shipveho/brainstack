"""Thin, stdlib-only client for the warm recall daemon (S3).

Deliberately imports nothing from `recall.core` / `qdrant_client` /
`fastembed`: this module runs inside the Claude Code UserPromptSubmit hook
on every prompt, and paying for those imports there would burn hundreds of
milliseconds before any work even starts. Only `socket`, `json`, `os`,
`time`, `pathlib` — stdlib.

The failure-reason mapping is not cosmetic; the hook branches on it:

  * `no_socket` / `connection_refused` — the daemon is DOWN. Nothing holds
    the Qdrant process lock, so falling back to the in-process retriever
    is safe.
  * `timeout` / `protocol_error` / `server_error` — the daemon is UP and
    holds the lock. An in-process fallback would just block on fcntl until
    the hook's own timeout kills it, so the hook reports `unavailable`
    instead of falling back.
"""

from __future__ import annotations

import errno
import json
import os
import socket
import time
from pathlib import Path

# Wire protocol version. Kept as a literal rather than imported from
# `recall.daemon` so this module stays free of the heavy import chain.
PROTOCOL_VERSION = 1

# Reasons that mean the daemon is DOWN — nothing is listening, so nothing
# holds the embedded store's exclusive lock and an in-process fallback is
# safe. Every other reason (`timeout`, `protocol_error`, `server_error`)
# means something IS listening: falling back would block on that lock and
# surface as "index is busy". The CLI, the MCP handler and the daemon's own
# stale-socket probe branch on this set. The hook keeps a superset of its
# own (adds `import_error`: this module itself missing is also "nothing is
# listening") because it must stay importable without the recall package.
DAEMON_DOWN_REASONS = frozenset({"no_socket", "connection_refused"})

_RECV_CHUNK = 65536


class DaemonUnavailable(RuntimeError):
    """Raised by every `daemon_client` call that could not get a good
    answer from the daemon.

    `reason` is one of: `no_socket`, `connection_refused`, `timeout`,
    `protocol_error`, `server_error`.
    """

    def __init__(self, reason: str, message: str = ""):
        self.reason = reason
        super().__init__(message or reason)


def _remaining_s(deadline: float) -> float:
    return deadline - time.monotonic()


def _arm_timeout(sock, deadline: float, message: str) -> None:
    """Give `sock` whatever is left of `deadline`, or give up.

    `budget_ms` is a hard wall on the WHOLE exchange, not on each syscall,
    so every step that can block re-checks the clock before it blocks and
    arms the socket with only the remainder. Raises
    `DaemonUnavailable("timeout", message)` when nothing is left.
    """
    remaining = _remaining_s(deadline)
    if remaining <= 0:
        raise DaemonUnavailable("timeout", message)
    sock.settimeout(remaining)


def request(socket_path: Path | str, payload: dict, *, budget_ms: int) -> dict:
    """Send one NDJSON request, return the parsed response dict, or raise
    `DaemonUnavailable` with the appropriate reason. Deadline-aware: never
    blocks longer than `budget_ms`, regardless of which failure mode is in
    play.
    """
    path = str(socket_path)
    deadline = time.monotonic() + (max(0, int(budget_ms)) / 1000.0)

    # Cheapest possible "daemon was never installed" answer: no connect, no
    # timeout, no syscall storm on every prompt.
    if not os.path.exists(path):
        raise DaemonUnavailable("no_socket", f"no daemon socket at {path}")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        _arm_timeout(sock, deadline, f"budget exhausted before connect to {path}")

        try:
            sock.connect(path)
        except TimeoutError as exc:
            raise DaemonUnavailable("timeout", f"connect to {path} timed out") from exc
        except ConnectionRefusedError as exc:
            raise DaemonUnavailable(
                "connection_refused", f"nothing listening at {path}: {exc}"
            ) from exc
        except FileNotFoundError as exc:
            raise DaemonUnavailable("no_socket", f"no daemon socket at {path}") from exc
        except OSError as exc:
            # ENOTSOCK (a stale regular file left at the socket path),
            # ENOENT racing the exists() check, EACCES on a foreign socket:
            # all mean "no daemon is answering here", i.e. the store is not
            # locked and the in-process fallback is safe.
            if exc.errno == errno.ENOENT:
                raise DaemonUnavailable("no_socket", f"no daemon socket at {path}") from exc
            raise DaemonUnavailable(
                "connection_refused", f"cannot reach daemon at {path}: {exc}"
            ) from exc

        line = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            _arm_timeout(sock, deadline, "budget exhausted before send")
            sock.sendall(line)
        except TimeoutError as exc:
            raise DaemonUnavailable("timeout", "send timed out") from exc
        except OSError as exc:
            raise DaemonUnavailable("protocol_error", f"send failed: {exc}") from exc

        buf = b""
        while b"\n" not in buf:
            _arm_timeout(
                sock, deadline,
                f"daemon at {path} did not answer within {budget_ms} ms",
            )
            try:
                chunk = sock.recv(_RECV_CHUNK)
            except TimeoutError as exc:
                raise DaemonUnavailable(
                    "timeout", f"daemon at {path} did not answer within {budget_ms} ms"
                ) from exc
            except OSError as exc:
                raise DaemonUnavailable("protocol_error", f"recv failed: {exc}") from exc
            if not chunk:
                raise DaemonUnavailable(
                    "protocol_error", "daemon closed the connection without answering"
                )
            buf += chunk

        head = buf.split(b"\n", 1)[0].strip()
        try:
            resp = json.loads(head.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise DaemonUnavailable(
                "protocol_error", f"daemon at {path} answered with non-JSON: {exc}"
            ) from exc
        if not isinstance(resp, dict):
            raise DaemonUnavailable(
                "protocol_error", "daemon answered with a non-object JSON value"
            )
        if resp.get("ok") is not True:
            message = str(resp.get("message") or resp.get("error") or "daemon error")
            raise DaemonUnavailable("server_error", message)
        return resp
    finally:
        try:
            sock.close()
        except OSError:
            pass


def query(
    prompt: str,
    *,
    k: int,
    socket_path: Path | str,
    budget_ms: int = 800,
    session_id: str = "",
    source_filter: str | None = None,
    type_filter: str | None = None,
    rerank: bool | None = None,
) -> dict:
    """Convenience wrapper: builds the `op: query` request and forwards to
    `request()`.
    """
    payload = {
        "v": PROTOCOL_VERSION,
        "op": "query",
        "prompt": prompt,
        "k": int(k),
        "session_id": session_id or "",
        "source": source_filter,
        "type": type_filter,
        "rerank": rerank,
        # Queue-wait hint: the daemon bounds its server-side wait by ~2x this.
        "budget_ms": int(budget_ms),
    }
    return request(socket_path, payload, budget_ms=budget_ms)


def status(socket_path: Path | str, *, timeout_s: float = 1.0) -> dict | None:
    """Probe the daemon. Returns `None` (never raises) when nothing is
    answering — `recall doctor` and `recall serve --status` both branch on
    that directly, and an exception would be a worse interface for a probe.
    """
    try:
        return request(
            socket_path,
            {"v": PROTOCOL_VERSION, "op": "status"},
            budget_ms=int(max(0.0, timeout_s) * 1000),
        )
    except DaemonUnavailable:
        return None


def shutdown(socket_path: Path | str, *, timeout_s: float = 2.0) -> bool:
    """Ask the daemon to shut down. Returns whether it acknowledged."""
    try:
        request(
            socket_path,
            {"v": PROTOCOL_VERSION, "op": "shutdown"},
            budget_ms=int(max(0.0, timeout_s) * 1000),
        )
    except DaemonUnavailable:
        return False
    return True


def reindex(socket_path: Path | str, *, timeout_s: float = 600.0) -> dict:
    """Ask the daemon to run one `refresh_once()` pass synchronously and
    return the change counts. A full cold build of a large brain can take
    minutes, hence the generous default budget.
    """
    return request(
        socket_path,
        {"v": PROTOCOL_VERSION, "op": "reindex"},
        budget_ms=int(max(0.0, timeout_s) * 1000),
    )


__all__ = [
    "DaemonUnavailable",
    "DAEMON_DOWN_REASONS",
    "PROTOCOL_VERSION",
    "request",
    "query",
    "status",
    "shutdown",
    "reindex",
]
