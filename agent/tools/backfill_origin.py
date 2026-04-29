"""One-shot migration helper: stamp `origin: "coding.tool_call"` on
existing episodes that predate PR1's schema unification.

Reads an episodic JSONL under sentinel lock, rewrites it atomically
(temp + os.replace) with `origin` added on every line that lacks it.
Lines with an explicit `origin` are passed through unchanged. Lines
that don't parse as JSON are dropped (consistent with the rest of the
pipeline's parser tolerance — see _read_jsonl in sdk.py and validate.py).

Usage:
    python -m agent.tools.backfill_origin --brain-root PATH \\
                                           [--namespace NS] \\
                                           [--dry-run]

Exit codes:
    0  success (or --dry-run reporting clean)
    2  episodic JSONL not found at the resolved path
    4  IO error during read or rewrite
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

# Make agent.memory.sdk importable for namespace path resolution + lock helper.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent.memory import sdk  # noqa: E402
from agent.memory._atomic import atomic_write_bytes  # noqa: E402

DEFAULT_ORIGIN = "coding.tool_call"


def _episodic_path(brain_root: str, namespace: str) -> str:
    # Validate namespace — without this the `--namespace` CLI arg flows
    # straight into a path concatenation, enabling path-traversal writes
    # via `--namespace ../../../etc/foo` (multi-tenant persona finding
    # on PR1). `sdk._episodic_path` itself does NOT validate; the regex
    # check lives in `_validate_namespace` which the public entry points
    # `append_episodic` etc. call but path-builders do not.
    sdk._validate_namespace(namespace)
    return sdk._episodic_path(namespace, brain_root)


def _backfill_lines(lines):
    """Return (rewritten_lines, stamped_count, dropped_count) for the input.

    Lines that parse as JSON and lack `origin` are stamped. Lines that
    parse but already have a non-empty `origin` are passed through
    unchanged. Lines that don't parse (or don't decode to a dict) are
    dropped — we count them and surface to the CLI so the operator can
    decide whether to abort. Dropping mirrors the rest of the pipeline's
    parser tolerance; reporting the count was a schema-persona finding.
    """
    out = []
    stamped = 0
    dropped = 0
    for raw in lines:
        s = raw.strip()
        if not s:
            continue
        try:
            row = json.loads(s)
        except json.JSONDecodeError:
            dropped += 1
            continue
        if not isinstance(row, dict):
            dropped += 1
            continue
        if not row.get("origin"):
            row["origin"] = DEFAULT_ORIGIN
            stamped += 1
        out.append(json.dumps(row))
    return out, stamped, dropped


def backfill(brain_root: str, namespace: str = "default",
             dry_run: bool = False) -> int:
    """Run the migration. Returns the count of entries that received a
    fresh `origin` stamp. Raises FileNotFoundError when the JSONL does
    not exist (callers translate to exit code 2).
    """
    path = _episodic_path(brain_root, namespace)
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    # Lock identity is the sentinel sibling, NOT the data file. Matches
    # the rest of the kernel (`_episodic_io.append_jsonl`) — sentinel-lock
    # survives os.replace because the lock-fd's inode never changes when
    # we atomically rewrite the data file.
    sentinel = path + ".lock"
    try:
        import fcntl  # POSIX
        have_flock = True
    except ImportError:  # pragma: no cover — Windows
        fcntl = None  # type: ignore[assignment]
        have_flock = False

    rewritten = None
    stamped = 0
    dropped = 0
    lock_fd = None
    try:
        if have_flock:
            lock_fd = os.open(sentinel, os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)  # type: ignore[union-attr]
        with open(path, "rb") as f:
            raw = f.read().decode("utf-8", errors="replace")
        rewritten, stamped, dropped = _backfill_lines(raw.splitlines())
        if dry_run or stamped == 0:
            return (stamped, dropped)
        # Trailing newline matches the append_jsonl convention.
        body = ("\n".join(rewritten) + "\n").encode("utf-8") if rewritten else b""
        atomic_write_bytes(path, body)
    finally:
        if lock_fd is not None and have_flock:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)  # type: ignore[union-attr]
            finally:
                os.close(lock_fd)
    return (stamped, dropped)


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(
        prog="backfill_origin",
        description="Stamp `origin: coding.tool_call` on legacy episodes.",
    )
    p.add_argument("--brain-root", required=True)
    p.add_argument("--namespace", default="default")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    try:
        stamped, dropped = backfill(args.brain_root, args.namespace, args.dry_run)
    except ValueError as e:
        # Raised by sdk._validate_namespace on path-traversal-shaped namespaces.
        sys.stderr.write(f"invalid namespace: {e}\n")
        return 3
    except FileNotFoundError as e:
        sys.stderr.write(f"episodic JSONL not found: {e}\n")
        return 2
    except OSError as e:
        sys.stderr.write(f"IO error: {type(e).__name__}: {e}\n")
        return 4

    verb = "would stamp" if args.dry_run else "stamped"
    msg = f"{verb} {stamped} episode(s) with origin={DEFAULT_ORIGIN}"
    if dropped:
        msg += f" (dropped {dropped} unparseable line(s))"
    sys.stdout.write(msg + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
