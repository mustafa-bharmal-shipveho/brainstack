"""Deterministic process exit for the exit-code-sensitive entry points.

The venv's grpcio (imported by qdrant_client) can abort during interpreter
teardown — `libc++abi: ... recursive_mutex lock failed`, exit 134, sometimes
SIGSEGV — after the program finished its work. Measured on 2026-09-04:
3 of 20 bare `import qdrant_client` runs at load 10+, 0 of 45 at load 3.

Who pays for it:

* the UserPromptSubmit hook — Claude Code adds a hook's stdout to the
  context only on exit 0, so a finished injection is discarded and the
  telemetry records a hit nobody saw;
* sync.sh, the dream lint step, `recall doctor`'s probe and the test suite,
  all of which read `recall`'s exit code as its verdict.

`hard_exit` flushes stdio, runs the registered atexit handlers (the embedded
Qdrant store's close lives there), then leaves through `os._exit`, which
skips the interpreter finalisation and C++ static destructors where the
abort happens. stdlib-only: the hook imports it on every prompt.
"""

from __future__ import annotations

import atexit
import os
import sys
from typing import NoReturn

# Module attribute (not a bare call) so tests can stub it: running the real
# handlers inside pytest would tear down the test process's own state.
_run_exitfuncs = atexit._run_exitfuncs


def exit_code_of(code: object) -> int:
    """`sys.exit` semantics: None -> 0, int -> itself (bools count as ints),
    anything else is a message for stderr and means 1."""
    if code is None:
        return 0
    if isinstance(code, bool):
        return int(code)
    if isinstance(code, int):
        return code
    try:
        print(code, file=sys.stderr)
    except Exception:  # noqa: BLE001 - a broken stderr must not block the exit
        pass
    return 1


def hard_exit(code: object = 0) -> NoReturn:
    """Flush, run atexit handlers, then `os._exit(code)`. Never returns."""
    rc = exit_code_of(code)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001 - closed pipe; nothing left to flush
            pass
    try:
        _run_exitfuncs()
    except Exception:  # noqa: BLE001 - a handler's failure must not block the exit
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001
            pass
    os._exit(rc)
