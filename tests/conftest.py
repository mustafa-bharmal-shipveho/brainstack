"""Test config.

Registers `pytest.mark.timeout` so tests that use it don't trip
PytestUnknownMarkWarning when `pytest-timeout` isn't installed (the marker
is a no-op without the plugin, but registration silences the warning and
documents intent).

Install pytest-timeout to actually enforce wall-clock bounds:
    pip install pytest-timeout

Also pins `RECALL_DAEMON_SOCKET` at a per-test tmp path for EVERY test
(autouse). Without it, any code path that resolves the daemon socket from
the environment (`recall.config.daemon_socket_path`, the CLI's daemon-first
`query` route, `recall doctor`, the Claude Code hook) would fall through to
the developer's LIVE socket at `~/.agent/runtime/recall.sock` and either
talk to a running daemon or block on it. Tests that genuinely need a daemon
bind their own socket under a short `/tmp` dir and override this value.
"""
import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "timeout(seconds): per-test wall-clock bound (no-op without pytest-timeout)",
    )


@pytest.fixture(autouse=True)
def _no_live_daemon_socket(tmp_path, monkeypatch):
    """Point every test at a nonexistent per-test daemon socket.

    The path deliberately does NOT exist: clients must resolve it to the
    `no_socket` reason and fall back to the in-process path, which is what
    a machine without the daemon installed sees.
    """
    monkeypatch.setenv("RECALL_DAEMON_SOCKET", str(tmp_path / "no-daemon.sock"))
