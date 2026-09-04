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

Scaffold: exception type + every function signature are real; the actual
socket I/O bodies raise `NotImplementedError("scaffold")` pending the
Development phase. See tests/recall/test_daemon_client.py.
"""

from __future__ import annotations

from pathlib import Path


class DaemonUnavailable(RuntimeError):
    """Raised by every `daemon_client` call that could not get a good
    answer from the daemon.

    `reason` is one of: `no_socket`, `connection_refused`, `timeout`,
    `protocol_error`, `server_error`.
    """

    def __init__(self, reason: str, message: str = ""):
        self.reason = reason
        super().__init__(message or reason)


def request(socket_path: Path | str, payload: dict, *, budget_ms: int) -> dict:
    """Send one NDJSON request, return the parsed response dict, or raise
    `DaemonUnavailable` with the appropriate reason. Deadline-aware: never
    blocks longer than `budget_ms`, regardless of which failure mode is in
    play.

    Scaffold: signature + docstring only. See
    tests/recall/test_daemon_client.py.
    """
    raise NotImplementedError("scaffold")


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

    Scaffold: signature + docstring only. See
    tests/recall/test_daemon_client.py::test_query_happy_path_returns_payload.
    """
    raise NotImplementedError("scaffold")


def status(socket_path: Path | str, *, timeout_s: float = 1.0) -> dict | None:
    """Probe the daemon. Returns `None` (never raises) when nothing is
    listening — `recall doctor` and `recall serve --status` both branch on
    that directly.

    Scaffold: signature + docstring only. See
    tests/recall/test_daemon_client.py::test_status_none_when_absent.
    """
    raise NotImplementedError("scaffold")


def shutdown(socket_path: Path | str, *, timeout_s: float = 2.0) -> bool:
    """Ask the daemon to shut down. Returns whether it acknowledged.

    Scaffold: signature + docstring only.
    """
    raise NotImplementedError("scaffold")


def reindex(socket_path: Path | str, *, timeout_s: float = 600.0) -> dict:
    """Ask the daemon to run one `refresh_once()` pass synchronously and
    return the change counts.

    Scaffold: signature + docstring only.
    """
    raise NotImplementedError("scaffold")


__all__ = [
    "DaemonUnavailable",
    "request",
    "query",
    "status",
    "shutdown",
    "reindex",
]
