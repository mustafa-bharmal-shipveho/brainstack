"""`recall runtime ...` subcommand group.

Wires the runtime CLI into the existing `recall` typer app. The existing
`recall` entry-point is at recall.cli:app; we expose a runtime subcommand
group here and register it from recall.cli.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from runtime.adapters.claude_code.config import RuntimeConfig
from runtime.adapters.claude_code.installer import (
    HookInstallReport,
    install_claude_code_hooks,
)
from runtime.core.events import load_events
from runtime.core.manifest import dump_manifest
from runtime.core.policy.defaults.lru import LRUPolicy
from runtime.core.replay import ReplayConfig, render_diff, replay

app = typer.Typer(
    name="runtime",
    help="brainstack context runtime: manifest + budgets + replay.",
    add_completion=False,
    no_args_is_help=True,
)


def _config() -> RuntimeConfig:
    return RuntimeConfig.load()


def _replay_config(cfg: RuntimeConfig, session_id: str = "current") -> ReplayConfig:
    return ReplayConfig(
        budgets=dict(cfg.budgets),
        policy=LRUPolicy(),
        session_id=session_id,
    )


@app.command("ls")
def cmd_ls(json_out: bool = typer.Option(False, "--json", help="emit JSON instead of human-readable")) -> None:
    """Show the current injection-set manifest."""
    cfg = _config()
    if not cfg.event_log_path.exists():
        typer.echo("(no events recorded yet)")
        raise typer.Exit(0)
    summary = replay(cfg.event_log_path, _replay_config(cfg))
    if not summary.manifests:
        typer.echo("(no manifest reconstructed)")
        raise typer.Exit(0)
    m = summary.manifests[-1]
    if json_out:
        typer.echo(dump_manifest(m))
        return
    typer.echo(f"session: {m.session_id}")
    typer.echo(f"turn:    {m.turn}")
    by_bucket: dict[str, int] = {}
    for it in m.items:
        by_bucket[it.bucket] = by_bucket.get(it.bucket, 0) + it.token_count
    typer.echo(f"budget:  {m.budget_used} / {m.budget_total} tokens")
    for bucket, used in sorted(by_bucket.items()):
        cap = cfg.budgets.get(bucket, 0)
        typer.echo(f"  {bucket:<12} {used:>6} / {cap:>6} tok ({len([it for it in m.items if it.bucket == bucket])} items)")
    typer.echo("")
    for it in sorted(m.items, key=lambda x: (x.bucket, x.id)):
        flag = "*" if it.pinned else " "
        typer.echo(f"  {flag} {it.id:<22} {it.bucket:<10} {it.token_count:>5} tok  {it.source_path}")


@app.command("timeline")
def cmd_timeline(
    full: bool = typer.Option(
        False, "--full",
        help="show every event (chronological firehose). Default is a compact summary.",
    ),
) -> None:
    """Compact summary of the session by default; --full for chronological detail.

    Default summary shows: lifecycle markers, eviction count + offending IDs,
    final per-bucket state. Survives the "one big turn" reality of typical
    Claude Code sessions where `--diff` between turns has nothing to compare.

    Use --full when debugging a specific event ("what added X at minute 23?").
    """
    from collections import Counter

    from runtime.core.events import load_events
    from runtime.core.manifest import InjectionItemSnapshot
    from runtime.core.replay import iter_engine_steps

    cfg = _config()
    if not cfg.event_log_path.exists():
        typer.echo("(no events recorded yet)")
        raise typer.Exit(0)
    events = load_events(cfg.event_log_path)
    if not events:
        typer.echo("(empty log)")
        raise typer.Exit(0)

    rcfg = _replay_config(cfg)

    if full:
        _render_timeline_full(cfg, list(iter_engine_steps(events, rcfg)))
    else:
        _render_timeline_summary(cfg, list(iter_engine_steps(events, rcfg)))


def _final_bucket_breakdown(cfg, manifest) -> str:
    by_bucket: dict[str, list[int]] = {}
    for it in manifest.items:
        slot = by_bucket.setdefault(it.bucket, [0, 0])
        slot[0] += 1
        slot[1] += it.token_count
    parts = []
    for b, (n, t) in sorted(by_bucket.items()):
        cap = cfg.budgets.get(b, 0)
        parts.append(f"{b}={n}items {t}/{cap}tok")
    return "  ".join(parts) if parts else "(empty)"


def _render_timeline_summary(cfg, steps) -> None:
    """Flight-recorder digest. Reads like a paragraph, not a stat block."""
    if not steps:
        return
    last = steps[-1]
    n_added = sum(len(s.added_ids) for s in steps)
    n_evicted = sum(len(s.evicted_ids) for s in steps)
    eviction_steps = [s for s in steps if s.evicted_ids]
    n_turns = last.manifest.turn + 1

    # Header: one-line summary of the session shape
    turn_word = "turn" if n_turns == 1 else "turns"
    typer.echo(f"Flight recorder for session \"{last.manifest.session_id}\" — {n_turns} {turn_word}, {len(steps)} events.")
    typer.echo("")

    # The headline narrative
    typer.echo(f"Claude saw {n_added} files/tool results during this session.")
    if n_evicted:
        breach_word = "breach" if len(eviction_steps) == 1 else "breaches"
        typer.echo(f"{n_evicted} were dropped because memory filled up ({len(eviction_steps)} budget {breach_word}).")
    else:
        typer.echo("No memory pressure — nothing was dropped.")
    typer.echo(f"{len(last.manifest.items)} items are still in memory.")

    # Recent breaches (the "why did Claude forget X?" debugging surface)
    if eviction_steps:
        typer.echo("")
        typer.echo("Recent budget breaches:")
        for s in eviction_steps[:5]:
            tool = s.event.tool_name or s.event.event
            n = len(s.evicted_ids)
            ids = ", ".join(eid[:10] for eid in s.evicted_ids[:4])
            extra = "" if n <= 4 else f" + {n - 4} more"
            item_word = "item" if n == 1 else "items"
            typer.echo(f"  • turn {s.manifest.turn} {tool}: dropped {n} {item_word} [{ids}{extra}]")
        if len(eviction_steps) > 5:
            typer.echo(f"  • …and {len(eviction_steps) - 5} more (run with --full to see every event)")

    # Current state, with % full so it's instantly readable
    typer.echo("")
    typer.echo("Memory now:")
    by_bucket: dict[str, list[int]] = {}
    for it in last.manifest.items:
        slot = by_bucket.setdefault(it.bucket, [0, 0])
        slot[0] += 1
        slot[1] += it.token_count
    for b, (n, t) in sorted(by_bucket.items()):
        cap = cfg.budgets.get(b, 0)
        pct = int(round(100 * t / cap)) if cap else 0
        item_word = "item" if n == 1 else "items"
        typer.echo(f"  {b:<11} {n:>3} {item_word:<5} {t:>6} / {cap:>6} tokens ({pct}% full)")

    typer.echo("")
    typer.echo("Run `recall runtime timeline --full` to see every event chronologically.")


def _render_timeline_full(cfg, steps) -> None:
    """Chronological firehose. Use when summary doesn't tell you enough."""
    from runtime.core.manifest import InjectionItemSnapshot

    last = None
    for step in steps:
        last = step
        ev = step.event

        if ev.event == "SessionStart":
            caps = "  ".join(f"{k}={v}" for k, v in sorted(cfg.budgets.items()))
            typer.echo(f"  SessionStart  budgets: {caps}")
            continue

        if ev.event == "UserPromptSubmit":
            typer.echo(f"  UserPromptSubmit  -> turn {step.manifest.turn}")
            continue

        if ev.event in {"Stop", "SubagentStop", "PostCompact", "Notification"}:
            typer.echo(f"  {ev.event}")
            continue

        if ev.event == "PostToolUse":
            snaps = [s for s in ev.items_added if isinstance(s, InjectionItemSnapshot)]
            if not snaps:
                typer.echo(f"  PostToolUse  {ev.tool_name}  (no items added)")
                continue
            for s in snaps:
                bucket_used = sum(
                    it.token_count for it in step.manifest.items if it.bucket == s.bucket
                )
                cap = cfg.budgets.get(s.bucket, 0)
                src = s.source_path if len(s.source_path) <= 38 else "…" + s.source_path[-37:]
                line = (
                    f"  + {ev.tool_name:<6} {src:<38} "
                    f"{s.token_count:>5} tok  {s.bucket} ({bucket_used}/{cap})"
                )
                if step.evicted_ids:
                    short_ids = ", ".join(eid[:10] for eid in step.evicted_ids)
                    line += f"  EVICTS [{short_ids}]"
                typer.echo(line)
            continue

        typer.echo(f"  {ev.event}")

    if last:
        typer.echo("")
        typer.echo(f"  final: {len(last.manifest.items)} items total  [{_final_bucket_breakdown(cfg, last.manifest)}]")


@app.command("budget")
def cmd_budget() -> None:
    """Show budget configuration and current usage."""
    cfg = _config()
    typer.echo(f"config: {cfg.config_path or '(defaults)'}")
    typer.echo("budgets per bucket (token cap):")
    for k, v in sorted(cfg.budgets.items()):
        typer.echo(f"  {k:<12} {v}")
    if cfg.event_log_path.exists():
        summary = replay(cfg.event_log_path, _replay_config(cfg))
        if summary.manifests:
            m = summary.manifests[-1]
            typer.echo("")
            typer.echo(f"current usage: {m.budget_used} tokens across {len(m.items)} items")


@app.command("replay")
def cmd_replay(
    session: str = typer.Argument("current", help="session id (currently 'current' is the only one)"),
    diff: str = typer.Option("", "--diff", help="show diff between two turns, e.g. '5:6'"),
) -> None:
    """Replay a session log; optionally show the diff between two turns."""
    cfg = _config()
    if not cfg.event_log_path.exists():
        typer.echo("no event log found")
        raise typer.Exit(1)
    summary = replay(cfg.event_log_path, _replay_config(cfg, session_id=session))
    if not summary.manifests:
        typer.echo("(empty replay)")
        raise typer.Exit(0)

    if diff:
        try:
            a_str, b_str = diff.split(":", 1)
            a_turn = int(a_str)
            b_turn = int(b_str)
        except ValueError:
            typer.echo(f"invalid --diff '{diff}'; use 'TURN_A:TURN_B'", err=True)
            raise typer.Exit(2)
        ma = next((m for m in summary.manifests if m.turn == a_turn), None)
        mb = next((m for m in summary.manifests if m.turn == b_turn), None)
        if ma is None or mb is None:
            typer.echo(f"turn {a_turn} or {b_turn} not in replay (turns: {[m.turn for m in summary.manifests]})", err=True)
            raise typer.Exit(2)
        typer.echo(render_diff(ma, mb))
        return

    typer.echo(f"replayed {summary.n_events} events across {summary.n_turns} turns")
    typer.echo("")
    for m in summary.manifests:
        typer.echo(f"  turn {m.turn:>3}: {len(m.items):>3} items, {m.budget_used:>5} tokens")


@app.command("pin")
def cmd_pin(item_id: str) -> None:
    """Mark an item as 'always-injected' (no eviction).

    Writes a synthetic Pin event to the event log so replay picks it up.
    The Engine handles pinning during replay via PinItem.
    """
    cfg = _config()
    from runtime.core.events import EVENT_LOG_SCHEMA_VERSION, EventRecord, append_event
    import time
    record = EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=int(time.time() * 1000),
        event="Pin",
        session_id="cli",
        turn=0,
        item_ids_added=[item_id],  # repurposed: list of ids to pin (see replay._translate)
    )
    append_event(cfg.event_log_path, record)
    typer.echo(f"pinned: {item_id}")


@app.command("unpin")
def cmd_unpin(item_id: str) -> None:
    """Remove a 'pinned' marker from an item."""
    cfg = _config()
    from runtime.core.events import EVENT_LOG_SCHEMA_VERSION, EventRecord, append_event
    import time
    record = EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=int(time.time() * 1000),
        event="Unpin",
        session_id="cli",
        turn=0,
        item_ids_evicted=[item_id],  # repurposed: list of ids to unpin
    )
    append_event(cfg.event_log_path, record)
    typer.echo(f"unpinned: {item_id}")


@app.command("evict")
def cmd_evict(item_id: str, reason: str = typer.Option("user-requested", "--reason")) -> None:
    """Force-evict an item by appending an explicit eviction event."""
    cfg = _config()
    from runtime.core.events import EVENT_LOG_SCHEMA_VERSION, EventRecord, append_event
    import time
    record = EventRecord(
        schema_version=EVENT_LOG_SCHEMA_VERSION,
        ts_ms=int(time.time() * 1000),
        event="PostToolUse",  # fold into a synthetic PostToolUse so replay handles it
        session_id="cli",
        turn=0,
        item_ids_evicted=[item_id],
    )
    append_event(cfg.event_log_path, record)
    typer.echo(f"evicted: {item_id} (reason: {reason})")


@app.command("install-hooks")
def cmd_install_hooks(
    settings: Path = typer.Option(
        Path("~/.claude/settings.json").expanduser(),
        "--settings", help="path to claude code settings.json",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="show changes without writing"),
) -> None:
    """Install brainstack runtime hooks into Claude Code's settings.json.

    Idempotent: re-running is a no-op. Adds entries that call this Python
    package's hook entrypoint for SessionStart, UserPromptSubmit,
    PostToolUse, Stop. Other hooks already present are preserved.
    """
    report = install_claude_code_hooks(settings_path=settings, dry_run=dry_run)
    typer.echo(report.summary())
    if dry_run:
        typer.echo("(dry-run: settings.json was NOT modified)")


__all__ = ["app"]
