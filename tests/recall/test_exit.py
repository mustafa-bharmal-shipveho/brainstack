"""`hard_exit`: a deterministic process exit for the hook and the CLI.

Under load, the venv's grpcio (imported by qdrant_client) can abort at
interpreter teardown — `libc++abi: ... recursive_mutex lock failed`, exit
134, sometimes SIGSEGV — AFTER the program finished its work (3 of 20 bare
`import qdrant_client` runs on 2026-09-04 at load 10+). For the
UserPromptSubmit hook that is fatal: Claude Code adds a hook's stdout to
the context only on exit 0, so a finished injection is discarded and shows
up in telemetry as a hit nobody saw. For sync.sh, dream and the tests it
turns a good run into a bad exit code.

`hard_exit` flushes stdio, runs the atexit handlers we rely on (the
embedded store's close), then `os._exit`s past the teardown that crashes.
The stubs here replace the process-level effects; a real `os._exit` or a
real `atexit._run_exitfuncs` inside pytest would end or corrupt the run.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from recall import _exit


class _Stop(Exception):
    pass


@pytest.fixture
def stubbed(monkeypatch):
    calls: list = []
    monkeypatch.setattr(_exit, "_run_exitfuncs", lambda: calls.append("atexit"))

    def _fake_os_exit(code):
        calls.append(("_exit", code))
        raise _Stop

    monkeypatch.setattr(_exit.os, "_exit", _fake_os_exit)
    return calls


def test_flushes_runs_atexit_handlers_then_exits_with_the_code(stubbed, capsys):
    print("last words", end="")  # unflushed on purpose

    with pytest.raises(_Stop):
        _exit.hard_exit(3)

    assert stubbed == ["atexit", ("_exit", 3)]
    assert capsys.readouterr().out == "last words"


@pytest.mark.parametrize(
    "code, expected",
    [(None, 0), (0, 0), (7, 7), (False, 0), (True, 1)],
)
def test_exit_code_follows_sys_exit_semantics(stubbed, code, expected):
    with pytest.raises(_Stop):
        _exit.hard_exit(code)
    assert stubbed[-1] == ("_exit", expected)


def test_a_message_prints_to_stderr_and_exits_1_like_sys_exit(stubbed, capsys):
    with pytest.raises(_Stop):
        _exit.hard_exit("boom")

    assert stubbed[-1] == ("_exit", 1)
    assert "boom" in capsys.readouterr().err


def test_an_atexit_handler_that_raises_does_not_prevent_the_exit(monkeypatch):
    calls: list = []

    def _boom():
        raise RuntimeError("handler blew up")

    monkeypatch.setattr(_exit, "_run_exitfuncs", _boom)

    def _fake_os_exit(code):
        calls.append(code)
        raise _Stop

    monkeypatch.setattr(_exit.os, "_exit", _fake_os_exit)

    with pytest.raises(_Stop):
        _exit.hard_exit(0)
    assert calls == [0]


def test_real_process_with_qdrant_loaded_exits_zero_after_printing():
    """End to end, in a real interpreter: import the module whose teardown
    aborts, print, hard_exit(0) — stdout is complete and the code is 0."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import qdrant_client\n"
            "from recall._exit import hard_exit\n"
            "print('done', end='')\n"
            "hard_exit(0)\n"
            "print('never')\n",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "done"


# --- wiring: the two exit-code-sensitive entry points use it ----------------


def test_recall_console_script_enters_through_main_not_the_typer_app():
    """`recall.cli:app` as the script target hands teardown to Typer/Click,
    which returns to the interpreter's normal (crashing) finalisation. The
    script must enter through `main`, which ends in `hard_exit`."""
    from pathlib import Path

    text = Path(__file__).resolve().parents[2].joinpath("pyproject.toml").read_text()
    assert 'recall = "recall.cli:main"' in text


def test_cli_main_hands_typers_exit_code_to_hard_exit(monkeypatch):
    from recall import cli

    seen: list = []

    def _fake_hard_exit(code):
        seen.append(code)
        raise _Stop

    monkeypatch.setattr(cli, "hard_exit", _fake_hard_exit)

    monkeypatch.setattr(sys, "argv", ["recall", "--help"])
    with pytest.raises(_Stop):
        cli.main()
    monkeypatch.setattr(sys, "argv", ["recall", "no-such-command-ever"])
    with pytest.raises(_Stop):
        cli.main()

    assert seen == [0, 2], seen


def test_hook_script_exits_through_hard_exit():
    """The hook is the consumer where a teardown abort costs the most: the
    injection it just printed is discarded. Pin the script's exit path."""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[2]
        / "runtime" / "adapters" / "claude_code" / "hooks.py"
    ).read_text()
    assert "hard_exit(main())" in src
    assert "sys.exit(main())" not in src
