"""recall CLI surface.

Subcommands: query, reindex, sources, doctor.

Init/migrate are stubs in this release — the safety design lives in
recall.migrate but the CLI wiring is deferred until the retriever's ranking
quality is validated on real prompts.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Optional

import typer

from recall import __version__
from recall.config import (
    Config,
    SourceConfig,
    cache_dir,
    config_path,
    load_config,
    resolve_brain_home,
)
from recall.core import HybridRetriever
from recall.index import build_index, load_index, needs_refresh
from recall.serialize import serialize_results
from recall.sources import discover_documents

app = typer.Typer(
    name="recall",
    help="Tool-agnostic memory retrieval for AI coding assistants.",
    add_completion=False,
    no_args_is_help=True,
)

# Runtime subcommand group (brainstack v0.2): manifest + budgets + replay.
# Imported lazily so the runtime/ tree's import errors never break the
# existing recall CLI.
try:
    from runtime.adapters.claude_code.cli import app as _runtime_app
    app.add_typer(_runtime_app, name="runtime")
except Exception:  # pragma: no cover
    pass


def _load_or_build(cfg: Config) -> tuple[Optional[object], bool]:
    """Returns (cache, fresh).

    `fresh=True` means we just rebuilt — the in-memory documents list is
    authoritative. `fresh=False` means the existing Qdrant collection is
    valid; the documents list is reconstructed from disk for shape-compat
    but the retriever should query the existing collection directly.
    """
    if needs_refresh(cfg.sources):
        return build_index(cfg.sources), True
    return load_index(cfg.sources), False


_serialize = serialize_results  # backwards-compat alias inside the module


@app.command()
def query(
    text: list[str] = typer.Argument(..., help="Query terms"),
    k: int = typer.Option(None, "--k", "-k", help="Number of results"),
    source: str = typer.Option(None, "--source", "-s", help="Filter by source name"),
    type: str = typer.Option(None, "--type", "-t", help="Filter by frontmatter type"),
    rerank: str = typer.Option(
        None,
        "--rerank",
        help='Override reranker: "cross_encoder" | "none". Default = config value.',
    ),
):
    """Search the brain for memories relevant to QUERY. Outputs JSON."""
    cfg = load_config()
    cache, fresh = _load_or_build(cfg)
    if cache is None or not cache.documents:
        typer.echo("[]")
        raise typer.Exit(code=0)

    # If we just rebuilt, pass docs in to be upserted (idempotent thanks to
    # uuid5(path) point ids). If the index is up to date, skip the embedding
    # step and let HybridRetriever query the existing collection directly.
    effective_reranker = rerank if rerank is not None else cfg.ranking.reranker
    retriever = HybridRetriever(
        documents=cache.documents if fresh else None,
        collections=[s.name for s in cfg.sources],
        embedder=cfg.ranking.embedder,
        sparse_embedder=cfg.ranking.sparse_embedder,
        reranker=effective_reranker,
        reranker_model=cfg.ranking.reranker_model,
        rerank_n=cfg.ranking.rerank_n,
    )

    query_str = " ".join(text)
    effective_k = k if k is not None else cfg.default_k
    results = retriever.query(
        query_str,
        k=effective_k,
        type_filter=type,
        source_filter=source,
    )
    typer.echo(json.dumps(_serialize(results), indent=2))


@app.command()
def reindex():
    """Rebuild the index cache from scratch."""
    cfg = load_config()
    cache = build_index(cfg.sources)
    typer.echo(
        f"Indexed {len(cache.documents)} documents across {len(cfg.sources)} source(s)."
    )


@app.command()
def sources():
    """List configured sources."""
    cfg = load_config()
    out = []
    for s in cfg.sources:
        path = Path(s.resolved_path)
        exists = path.exists()
        n_docs = sum(1 for _ in discover_documents(s)) if exists else 0
        out.append(
            {
                "name": s.name,
                "path": s.path,
                "resolved_path": s.resolved_path,
                "frontmatter": s.frontmatter,
                "exists": exists,
                "documents": n_docs,
            }
        )
    typer.echo(json.dumps(out, indent=2))


@app.command()
def remember(
    text: str = typer.Argument(..., help="The lesson to write. Plain markdown."),
    name: str = typer.Option("", "--as", help="explicit slug. Default: derived from first line."),
    description: str = typer.Option("", "--description", help="one-line summary. Default: first line of text."),
    overwrite: bool = typer.Option(False, "--overwrite", help="overwrite an existing lesson with the same slug"),
    brain_root: Path = typer.Option(
        Path("~/.agent").expanduser(),
        "--brain-root",
        help="brainstack memory root. Default: ~/.agent",
    ),
):
    """Permanently remember a lesson — auto-loaded on every future session.

    Writes a markdown file to ~/.agent/memory/semantic/lessons/<slug>.md
    with frontmatter. Different from `recall runtime add` which is
    session-scoped (one prompt only).

    Examples:
        recall remember "always use /agent-team for development"
        recall remember "use SELECT FOR UPDATE SKIP LOCKED for queue claims" --as postgres-locking
    """
    from recall.remember import write_lesson
    try:
        path = write_lesson(
            text=text,
            name=name or None,
            description=description or None,
            brain_root=brain_root,
            overwrite=overwrite,
        )
    except (FileNotFoundError, FileExistsError, ValueError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2)
    typer.echo(f"remembered: {path}")
    typer.echo("(brainstack will auto-load this lesson on every future session)")


@app.command()
def forget(
    query: str = typer.Argument(..., help="lesson name, basename, or substring to forget"),
    brain_root: Path = typer.Option(
        Path("~/.agent").expanduser(),
        "--brain-root",
        help="brainstack memory root. Default: ~/.agent",
    ),
):
    """Archive a permanent lesson out of brainstack's memory.

    Resolves the query against ~/.agent/memory/semantic/lessons/ (basename
    then substring), then moves the matched lesson to
    ~/.agent/memory/semantic/archived/<timestamp>-<name>.md so it's
    recoverable. Multi-match prints candidates and exits non-zero.

    Examples:
        recall forget agent-team
        recall forget postgres-locking
    """
    from recall.forget import archive_lesson
    result = archive_lesson(query, brain_root=brain_root)
    if result.archived_path:
        typer.echo(f"forgotten: {result.archived_path.name}")
        typer.echo(f"  archived to: {result.archived_path}")
        typer.echo("  (recover with: mv that path back into ~/.agent/memory/semantic/lessons/)")
        return
    if result.candidates:
        typer.echo(f"'{query}' matched {len(result.candidates)} lessons (be more specific):", err=True)
        for c in result.candidates:
            typer.echo(f"  {c}", err=True)
    else:
        typer.echo(f"no lesson matches '{query}' under {brain_root}/memory/semantic/lessons/", err=True)
    raise typer.Exit(2)


@app.command()
def doctor():
    """Diagnose configuration and dependency issues."""
    issues: list[str] = []
    notes: list[str] = []

    notes.append(f"recall version: {__version__}")
    notes.append(f"Python: {sys.version.split()[0]}")

    # Config
    cp = config_path()
    notes.append(f"Config path: {cp}")
    if cp.exists():
        notes.append("Config: present")
    else:
        notes.append("Config: missing (will be auto-created on first run)")

    # Brain home
    brain = resolve_brain_home()
    notes.append(f"BRAIN_HOME: {brain}")
    if brain.exists():
        notes.append(f"BRAIN_HOME exists ({sum(1 for _ in brain.rglob('*.md'))} markdown files)")
    else:
        notes.append("BRAIN_HOME does not exist yet")

    # Cache dir
    cd = cache_dir()
    notes.append(f"Cache dir: {cd}")

    # Sources
    try:
        cfg = load_config()
        for s in cfg.sources:
            if not Path(s.resolved_path).exists():
                issues.append(
                    f"Source '{s.name}' path missing: {s.path} → {s.resolved_path}"
                )
    except Exception as e:
        issues.append(f"Failed to load config: {e}")

    # Required deps for retrieval (Qdrant + FastEmbed)
    try:
        import qdrant_client  # noqa: F401
        from importlib.metadata import version as _ver
        try:
            qver = _ver("qdrant-client")
        except Exception:
            qver = "unknown"
        notes.append(f"qdrant-client: installed ({qver})")
    except ImportError:
        issues.append("qdrant-client: NOT installed (required for retrieval)")
    try:
        import fastembed  # noqa: F401
        notes.append("fastembed: installed")
    except ImportError:
        issues.append("fastembed: NOT installed (required for retrieval)")

    # Embedded Qdrant data dir
    qdrant_data = cd / "qdrant"
    if qdrant_data.exists():
        notes.append(f"Qdrant store: {qdrant_data} (present)")
    else:
        notes.append(
            f"Qdrant store: {qdrant_data} (will be created on first reindex)"
        )
    notes.append(
        "First reindex downloads BAAI/bge-base-en-v1.5 (~440 MB) "
        "to ~/.cache/fastembed/ — one-time."
    )

    if importlib.util.find_spec("mcp") is None:
        notes.append("mcp: not installed (recall-mcp unavailable)")
    else:
        notes.append("mcp: installed")

    typer.echo("== recall doctor ==")
    for n in notes:
        typer.echo(f"  {n}")
    if issues:
        typer.echo("\nIssues:")
        for i in issues:
            typer.echo(f"  ! {i}")
        raise typer.Exit(code=1)
    typer.echo("\nNo issues detected.")


@app.command()
def pending(
    refresh: bool = typer.Option(
        False, "--refresh",
        help="Force-regenerate <brain>/PENDING_REVIEW.md before printing.",
    ),
    review: bool = typer.Option(
        False, "--review",
        help="Open the candidate triage flow (delegates to list_candidates.py).",
    ),
    brain: Optional[Path] = typer.Option(
        None, "--brain",
        help="Brain root (default: $BRAIN_ROOT or ~/.agent).",
    ),
):
    """Print the pending-review summary (~/.agent/PENDING_REVIEW.md).

    The summary is generated by `agent/tools/render_pending_summary.py`
    and contains pending candidate counts (per namespace), drift status,
    and sync staleness. Empty days print nothing — the source file holds
    a one-liner that's suppressed for cleanliness.
    """
    import os
    import subprocess
    from pathlib import Path as _Path

    brain_root = brain or _Path(os.environ.get("BRAIN_ROOT", str(_Path.home() / ".agent")))
    if not brain_root.is_dir():
        typer.echo(f"recall pending: brain not found: {brain_root}", err=True)
        raise typer.Exit(code=2)

    summary_path = brain_root / "PENDING_REVIEW.md"
    renderer = brain_root / "tools" / "render_pending_summary.py"

    # Force-regenerate (or auto-regenerate if file is missing / stale > 5min)
    needs_refresh = refresh or not summary_path.is_file()
    if not needs_refresh:
        import time
        try:
            age = time.time() - summary_path.stat().st_mtime
            if age > 300:
                needs_refresh = True
        except OSError:
            needs_refresh = True
    if needs_refresh and renderer.is_file():
        try:
            subprocess.run(
                [sys.executable, str(renderer), "--brain", str(brain_root)],
                check=False, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass  # render failure is non-fatal — fall through to whatever's on disk

    # Review mode: hand off to the interactive triage REPL.
    # The REPL refuses to run without a TTY (structural guarantee that
    # the user, not an LLM, decides each candidate). Mustafa 2026-05-04:
    # "i want the users to be able to accept or reject" — a Claude
    # session previously rejected 22 candidates without prompting; the
    # REPL makes that structurally impossible.
    if review:
        triage = brain_root / "tools" / "triage_candidates.py"
        if triage.is_file():
            os.execv(sys.executable, [sys.executable, str(triage), "--brain", str(brain_root)])
        typer.echo(
            "recall pending: triage_candidates.py missing; run `./install.sh --upgrade`",
            err=True,
        )
        raise typer.Exit(code=2)

    # Default: print the summary
    if summary_path.is_file():
        try:
            typer.echo(summary_path.read_text(), nl=False)
        except OSError as e:
            typer.echo(f"recall pending: read failed {summary_path}: {e}", err=True)
            raise typer.Exit(code=1)
    else:
        typer.echo("recall pending: no summary file (brain may be brand new)")


@app.command()
def stats(
    since: str = typer.Option(
        "", "--since",
        help="Window: '7d', '24h', '30m', '60s', or YYYY-MM-DD. Default: all-time.",
    ),
    session_current: bool = typer.Option(
        False, "--session-current",
        help="Override --since: window starts at the most recent SessionStart event.",
    ),
    json_out: bool = typer.Option(
        False, "--json",
        help="Emit the StatsReport as JSON instead of the human-readable view.",
    ),
    no_tools: bool = typer.Option(
        False, "--no-tools",
        help="Skip Claude Code transcript scanning. Faster on brains with hundreds of session files.",
    ),
    transcripts_dir: Optional[Path] = typer.Option(
        None, "--transcripts-dir",
        help="Override Claude Code transcripts root (default: ~/.claude/projects).",
    ),
):
    """Auto-recall ROI + cross-source visibility.

    Reads ~/.agent/runtime/logs/events.log.jsonl for AutoRecall events
    (per-prompt retrieval injections) AND scans Claude Code transcripts
    at ~/.claude/projects/<slug>/<sid>.jsonl for tool_use blocks
    (model-driven tool calls — MCP servers, Bash, Edit, etc.).

    The MCP-call breakdown is raw counts, namespaced by server. We don't
    try to infer "is the model calling the right MCP for this question
    type" — that's org-specific (your CLAUDE.md routing rules vary) and
    a generic regex classifier proved unreliable in practice.

    Use --no-tools when you only want the auto-recall counters fast (the
    transcript scan reads many JSONL files per call; cheap but not free
    on brains with months of history).
    """
    import json as _json

    from recall.stats import (
        StatsReport,
        aggregate_events,
        aggregate_tool_calls,
        parse_since,
        render_human,
    )
    from runtime.adapters.claude_code.config import RuntimeConfig

    runtime_cfg = RuntimeConfig.load()
    log_path = runtime_cfg.event_log_path

    if session_current:
        since_ts_ms = _session_current_ts_ms(log_path) if log_path.exists() else None
    else:
        try:
            since_ts_ms = parse_since(since)
        except ValueError as e:
            typer.echo(f"recall stats: {e}", err=True)
            raise typer.Exit(code=2)

    # Auto-recall events: optional. A user who hasn't enabled auto-recall
    # but has Claude Code transcripts should still see the tool-call /
    # coverage breakdown. Codex 2026-05-05 P2.
    if log_path.exists():
        report = aggregate_events(log_path, since_ts_ms=since_ts_ms)
    else:
        report = StatsReport(window_start_ts_ms=since_ts_ms)

    # Cross-source: scan transcripts for MCP / builtin tool calls in the
    # same window. Bucket by namespace (mcp__minerva__* etc). Raw counts
    # only — interpretation (e.g. "should the model have called X here")
    # is org-specific and intentionally not computed here.
    if not no_tools:
        td = transcripts_dir or (Path.home() / ".claude" / "projects")
        all_calls = aggregate_tool_calls(td, since_ts_ms=since_ts_ms)
        report.mcp_calls = {k: v for k, v in all_calls.items() if k.startswith("mcp__")}
        report.tool_calls_other = {k: v for k, v in all_calls.items() if not k.startswith("mcp__")}

    if json_out:
        # Re-key from StatsReport dataclass to plain dict (top_sources tuple
        # → list[list] for stable JSON serialization).
        from dataclasses import asdict
        data = asdict(report)
        data["top_sources"] = [list(t) for t in data["top_sources"]]
        data["top_paths"] = [list(t) for t in data["top_paths"]]
        typer.echo(_json.dumps(data, indent=2))
        return
    typer.echo(render_human(report))


def _session_current_ts_ms(log_path: "Path") -> int | None:
    """Find the most recent SessionStart event's timestamp."""
    from runtime.core.events import load_events
    events = load_events(log_path)
    starts = [e.ts_ms for e in events if e.event == "SessionStart"]
    return max(starts) if starts else None


def main():
    app()


if __name__ == "__main__":
    main()
