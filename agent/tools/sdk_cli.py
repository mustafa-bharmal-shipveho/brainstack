"""Argparse CLI wrapper around `agent.memory.sdk`.

Lets non-Python consumers (e.g. the TypeScript kernel-client) drive
episodic writes / semantic reads / policy R-W via subprocess calls.

Subcommands:
    append-episodic  --namespace NS --event '<json>' [--event-stdin]
    query-semantic   --namespace NS [--query Q] [--k N]
    read-policy      --namespace NS
    write-policy     --namespace NS --policy '<yaml-or-json>' [--policy-stdin]

All commands accept `--brain-root PATH`. Results are JSON on stdout.
On error, JSON `{"error": "...", "code": "..."}` is written to stderr
and the process exits non-zero.

Exit codes:
    0  success
    2  invalid args / parse error
    3  invalid namespace
    4  IO error (file system, locks)
    5  internal / unexpected error
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Optional

# Make `agent.memory.sdk` importable when the CLI is invoked from any cwd.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent.memory import sdk  # noqa: E402


# --- error helpers ---------------------------------------------------

class _CliError(Exception):
    def __init__(self, code: str, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


def _emit_error(code: str, message: str, exit_code: int) -> int:
    sys.stderr.write(json.dumps({"error": message, "code": code}) + "\n")
    sys.stderr.flush()
    return exit_code


def _emit_ok(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


# --- helpers ---------------------------------------------------------

def _read_stdin() -> str:
    data = sys.stdin.read()
    if not data:
        raise _CliError("invalid_args", "stdin was empty", 2)
    return data


def _parse_event(args: argparse.Namespace) -> dict:
    if args.event_stdin:
        text = _read_stdin()
    elif args.event is not None:
        text = args.event
    else:
        raise _CliError("invalid_args", "one of --event or --event-stdin required", 2)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise _CliError("invalid_args", f"event is not valid JSON: {e}", 2) from e
    if not isinstance(obj, dict):
        raise _CliError("invalid_args", "event JSON must be an object", 2)
    return obj


def _parse_policy(args: argparse.Namespace) -> dict:
    if args.policy_stdin:
        text = _read_stdin()
    elif args.policy is not None:
        text = args.policy
    else:
        raise _CliError("invalid_args", "one of --policy or --policy-stdin required", 2)
    text = text.strip()
    if not text:
        raise _CliError("invalid_args", "policy text is empty", 2)
    # Auto-detect: leading `{` => JSON, else YAML.
    if text.lstrip().startswith("{"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as e:
            raise _CliError("invalid_args", f"policy is not valid JSON: {e}", 2) from e
    else:
        try:
            import yaml  # type: ignore
        except ImportError as e:  # pragma: no cover - env-dependent
            raise _CliError(
                "invalid_args",
                "policy looks like YAML but PyYAML is not installed; "
                "pass JSON instead",
                2,
            ) from e
        try:
            obj = yaml.safe_load(text)
        except Exception as e:
            raise _CliError("invalid_args", f"policy is not valid YAML: {e}", 2) from e
    if not isinstance(obj, dict):
        raise _CliError("invalid_args", "policy must be a mapping/object", 2)
    return obj


# --- subcommand handlers --------------------------------------------

def _cmd_append_episodic(args: argparse.Namespace) -> int:
    event = _parse_event(args)
    try:
        stamped = sdk.append_episodic(args.namespace, event, brain_root=args.brain_root)
    except ValueError as e:
        msg = str(e)
        if "namespace" in msg:
            return _emit_error("invalid_namespace", msg, 3)
        return _emit_error("invalid_args", msg, 2)
    except OSError as e:
        return _emit_error("io_error", f"{type(e).__name__}: {e}", 4)
    _emit_ok(stamped)
    return 0


def _cmd_query_semantic(args: argparse.Namespace) -> int:
    try:
        rows = sdk.query_semantic(
            args.namespace,
            query=args.query,
            k=args.k,
            brain_root=args.brain_root,
        )
    except ValueError as e:
        msg = str(e)
        if "namespace" in msg:
            return _emit_error("invalid_namespace", msg, 3)
        return _emit_error("invalid_args", msg, 2)
    except OSError as e:
        return _emit_error("io_error", f"{type(e).__name__}: {e}", 4)
    _emit_ok(rows)
    return 0


def _cmd_read_policy(args: argparse.Namespace) -> int:
    try:
        policy = sdk.read_policy(args.namespace, brain_root=args.brain_root)
    except ValueError as e:
        msg = str(e)
        if "namespace" in msg:
            return _emit_error("invalid_namespace", msg, 3)
        return _emit_error("invalid_args", msg, 2)
    except OSError as e:
        return _emit_error("io_error", f"{type(e).__name__}: {e}", 4)
    _emit_ok(policy)
    return 0


def _cmd_write_policy(args: argparse.Namespace) -> int:
    policy = _parse_policy(args)
    try:
        sdk.write_policy(args.namespace, policy, brain_root=args.brain_root)
    except ValueError as e:
        msg = str(e)
        if "namespace" in msg:
            return _emit_error("invalid_namespace", msg, 3)
        return _emit_error("invalid_args", msg, 2)
    except OSError as e:
        return _emit_error("io_error", f"{type(e).__name__}: {e}", 4)
    _emit_ok({"ok": True})
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    """PR5: per-namespace + aggregate counts for the `agentry brain stats` CLI.

    `--namespace` is OPTIONAL (unlike the other subcommands) — without it,
    stats walks every namespace it finds under `<brain_root>/memory/episodic/`.
    """
    try:
        ns: Optional[str] = args.namespace if args.namespace else None
        result = sdk.stats(namespace=ns, brain_root=args.brain_root)
    except ValueError as e:
        msg = str(e)
        if "namespace" in msg:
            return _emit_error("invalid_namespace", msg, 3)
        return _emit_error("invalid_args", msg, 2)
    except OSError as e:
        return _emit_error("io_error", f"{type(e).__name__}: {e}", 4)
    _emit_ok(result)
    return 0


# --- argparse setup --------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sdk_cli",
        description="CLI surface for agent.memory.sdk (cross-language consumers).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def _add_common(s: argparse.ArgumentParser) -> None:
        s.add_argument("--namespace", required=True, help="brain namespace")
        s.add_argument("--brain-root", default=None, help="brain root path override")

    a = sub.add_parser("append-episodic", help="append an event to episodic memory")
    _add_common(a)
    g = a.add_mutually_exclusive_group()
    g.add_argument("--event", default=None, help="event as a JSON string")
    g.add_argument("--event-stdin", action="store_true", help="read event JSON from stdin")
    a.set_defaults(handler=_cmd_append_episodic)

    q = sub.add_parser("query-semantic", help="query semantic lessons")
    _add_common(q)
    q.add_argument("--query", default=None, help="optional substring filter")
    q.add_argument("--k", type=int, default=10, help="max results (default 10)")
    q.set_defaults(handler=_cmd_query_semantic)

    r = sub.add_parser("read-policy", help="read namespace policy")
    _add_common(r)
    r.set_defaults(handler=_cmd_read_policy)

    w = sub.add_parser("write-policy", help="write namespace policy")
    _add_common(w)
    g2 = w.add_mutually_exclusive_group()
    g2.add_argument("--policy", default=None, help="policy as JSON or YAML string")
    g2.add_argument("--policy-stdin", action="store_true", help="read policy from stdin")
    w.set_defaults(handler=_cmd_write_policy)

    # PR5: stats subcommand — namespace is optional here (unlike the others).
    s = sub.add_parser("stats", help="per-namespace + aggregate brain counts")
    s.add_argument("--namespace", default=None,
                   help="restrict to one namespace (default: walk all)")
    s.add_argument("--brain-root", default=None, help="brain root path override")
    s.set_defaults(handler=_cmd_stats)

    return p


def main(argv: Optional[list] = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        # argparse exits 2 on parse errors; preserve code but emit JSON.
        code = e.code if isinstance(e.code, int) else 2
        if code != 0:
            sys.stderr.write(json.dumps({"error": "argparse error", "code": "invalid_args"}) + "\n")
        return code if isinstance(code, int) else 2

    try:
        return args.handler(args)
    except _CliError as e:
        return _emit_error(e.code, str(e), e.exit_code)
    except Exception as e:  # pragma: no cover - defensive
        return _emit_error("internal", f"{type(e).__name__}: {e}", 5)


if __name__ == "__main__":
    sys.exit(main())
