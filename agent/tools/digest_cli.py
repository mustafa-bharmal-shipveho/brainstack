"""CLI for the session-digest layer.

Subcommands:

    digest_cli.py provider list
        Print each registered provider with availability marker + fix-it
        text. Exit 0 if any provider is ready, non-zero otherwise.

    digest_cli.py backfill [--source claude|codex|both]
                            [--limit N] [--dry-run]
        Walk historical sessions and produce per-session digests. Idempotent
        via content-SHA sidecar — safe to re-run.

    digest_cli.py incremental
        Same as `backfill` but intended for the hourly LaunchAgent.
        Always processes BOTH sources, no limit.

    digest_cli.py status
        Print sidecar stats + counts of episodic lines + markdown
        files on disk.

Auth: uses whatever LLM CLI the user already has authed (claude -p or
codex exec). No separate API key needed.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Path setup so we can import the adapter and providers.
_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent))

import claude_session_digest_adapter as adapter  # type: ignore
from llm_providers import PROVIDERS, resolve_provider  # type: ignore
from llm_providers.base import LLMError, ProviderNotAvailable  # type: ignore


def _brain_root() -> Path:
    env = os.environ.get("BRAIN_ROOT")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".agent"


def _projects_root() -> Path:
    return Path.home() / ".claude" / "projects"


def _codex_root() -> Path:
    return Path.home() / ".codex"


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def _cmd_provider_list(args) -> int:
    """Print providers with availability marker. Exit 0 if any
    available; non-zero otherwise so install.sh --setup-digests can
    branch on it."""
    any_available = False
    for name, p in PROVIDERS.items():
        ok, reason = p.is_available()
        marker = "✓" if ok else "✗"
        if ok:
            any_available = True
            print(f"  {marker} {name:<12}  default_model={p.default_model}")
        else:
            print(f"  {marker} {name:<12}  {reason}")
    if not any_available:
        print()
        print("No LLM provider available for digests. To enable:")
        print("  • Claude:  run `claude setup-token` "
              "(uses your Claude subscription)")
        print("  • Codex:   run `codex login` "
              "(uses your Codex/ChatGPT account)")
        print("Then re-run `digest_cli.py provider list`.")
        return 1
    return 0


def _cmd_backfill(args, *, source_override: str | None = None) -> int:
    brain = _brain_root()
    src = source_override or args.source

    if args.dry_run:
        # Walk only — no LLM calls, no writes. Useful sanity check.
        from claude_session_digest_adapter import (  # type: ignore
            iter_claude_sessions, iter_codex_sessions,
        )
        n_claude = n_codex = 0
        if src in ("both", "claude"):
            for _ns in iter_claude_sessions(_projects_root()):
                n_claude += 1
                if args.limit and (n_claude + n_codex) >= args.limit:
                    break
        if src in ("both", "codex") and not (
            args.limit and (n_claude + n_codex) >= args.limit
        ):
            for _ns in iter_codex_sessions(_codex_root()):
                n_codex += 1
                if args.limit and (n_claude + n_codex) >= args.limit:
                    break
        print(f"dry-run: would process claude={n_claude} codex={n_codex}")
        return 0

    try:
        provider = resolve_provider(args.provider)
    except (ValueError, ProviderNotAvailable) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    print(f"using provider: {provider.name} "
          f"(default_model={provider.default_model})")

    # Honor --limit by wrapping the adapter's iterators (cheap hack:
    # set a per-source limit by mutating the adapter's iter functions
    # via a wrapper. Simpler: pass an upper-bound and short-circuit.)
    # Adapter walks sources in order; we wrap with a counter.
    if args.limit:
        _wrap_limit(adapter, args.limit)

    projects_root = _projects_root() if src in ("both", "claude") else None
    codex_root = _codex_root() if src in ("both", "codex") else None

    try:
        stats = adapter.backfill(
            brain_root=brain,
            projects_root=projects_root,
            codex_root=codex_root,
            provider=provider,
            log=print,
        )
    except ProviderNotAvailable as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    print("---")
    print(f"discovered:    {stats['discovered']}")
    print(f"written:       {stats['digests_written']}")
    print(f"skipped (sha): {stats['skipped_idempotent']}")
    print(f"failed:        {stats['failed']}")
    return 0


def _wrap_limit(adapter_mod, limit: int) -> None:
    """Decorate the adapter's source iterators to stop after `limit`
    yielded sessions total. Implemented via a shared counter so claude
    + codex together respect the limit. Used by `backfill --limit N`."""
    original_claude = adapter_mod.iter_claude_sessions
    original_codex = adapter_mod.iter_codex_sessions
    state = {"n": 0}

    def _bounded(orig):
        def gen(*a, **kw):
            for item in orig(*a, **kw):
                if state["n"] >= limit:
                    return
                state["n"] += 1
                yield item
        return gen

    adapter_mod.iter_claude_sessions = _bounded(original_claude)
    adapter_mod.iter_codex_sessions = _bounded(original_codex)


def _cmd_incremental(args) -> int:
    """Same as backfill --source both, no limit. Designed for the
    hourly LaunchAgent."""
    args.source = "both"
    args.limit = 0
    args.dry_run = False
    return _cmd_backfill(args)


def _cmd_status(args) -> int:
    brain = _brain_root()
    sidecar = brain / "memory" / "episodic" / "digests" / "_imported.jsonl"
    ep = brain / "memory" / "episodic" / "digests" / "AGENT_LEARNINGS.jsonl"
    md_dir = brain / "memory" / "semantic" / "digests"

    n_sidecar = 0
    if sidecar.is_file():
        n_sidecar = sum(1 for l in sidecar.read_text().splitlines()
                        if l.strip())
    n_ep = 0
    if ep.is_file():
        n_ep = sum(1 for l in ep.read_text().splitlines() if l.strip())
    n_md = 0
    if md_dir.is_dir():
        n_md = len(list(md_dir.glob("*.md")))

    print(f"brain_root:        {brain}")
    print(f"sidecar entries:   {n_sidecar}  ({sidecar})")
    print(f"episodic lines:    {n_ep}  ({ep})")
    print(f"markdown digests:  {n_md}  ({md_dir})")
    return 0


# ---------------------------------------------------------------------------
# argparse + dispatch
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="digest_cli")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("provider")
    sp_sub = sp.add_subparsers(dest="subcmd")
    sp_list = sp_sub.add_parser("list")

    sb = sub.add_parser("backfill")
    sb.add_argument("--source", choices=["claude", "codex", "both"],
                    default="both")
    sb.add_argument("--limit", type=int, default=0)
    sb.add_argument("--dry-run", action="store_true")
    sb.add_argument("--provider", default=None,
                    help="Override resolved provider (claude-code|codex|...)")

    si = sub.add_parser("incremental")
    si.add_argument("--provider", default=None)

    st = sub.add_parser("status")

    args = p.parse_args(argv)

    if args.cmd == "provider":
        if args.subcmd == "list":
            return _cmd_provider_list(args)
        sp.print_help()
        return 2
    if args.cmd == "backfill":
        return _cmd_backfill(args)
    if args.cmd == "incremental":
        # incremental shares backfill flags; set defaults
        args.source = "both"
        args.limit = 0
        args.dry_run = False
        return _cmd_backfill(args)
    if args.cmd == "status":
        return _cmd_status(args)

    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
