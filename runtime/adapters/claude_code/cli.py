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
    """Mark an item as 'always-injected' (no eviction)."""
    cfg = _config()
    pin_file = cfg.log_dir / "pinned.json"
    pin_file.parent.mkdir(parents=True, exist_ok=True)
    current: list[str] = []
    if pin_file.exists():
        try:
            current = json.loads(pin_file.read_text())
        except json.JSONDecodeError:
            current = []
    if item_id not in current:
        current.append(item_id)
        pin_file.write_text(json.dumps(sorted(set(current)), indent=2))
    typer.echo(f"pinned: {item_id}")


@app.command("unpin")
def cmd_unpin(item_id: str) -> None:
    """Remove a 'pinned' marker from an item."""
    cfg = _config()
    pin_file = cfg.log_dir / "pinned.json"
    if not pin_file.exists():
        typer.echo(f"(no pins to remove)")
        return
    try:
        current = json.loads(pin_file.read_text())
    except json.JSONDecodeError:
        current = []
    current = [x for x in current if x != item_id]
    pin_file.write_text(json.dumps(sorted(set(current)), indent=2))
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
