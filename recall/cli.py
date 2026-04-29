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


def _load_or_build(cfg: Config):
    if needs_refresh(cfg.sources):
        return build_index(cfg.sources)
    return load_index(cfg.sources)


_serialize = serialize_results  # backwards-compat alias inside the module


@app.command()
def query(
    text: list[str] = typer.Argument(..., help="Query terms"),
    k: int = typer.Option(None, "--k", "-k", help="Number of results"),
    source: str = typer.Option(None, "--source", "-s", help="Filter by source name"),
    type: str = typer.Option(None, "--type", "-t", help="Filter by frontmatter type"),
):
    """Search the brain for memories relevant to QUERY. Outputs JSON."""
    cfg = load_config()
    cache = _load_or_build(cfg)
    if cache is None or not cache.documents:
        typer.echo("[]")
        raise typer.Exit(code=0)

    retriever = HybridRetriever(
        cache.documents,
        bm25_weight=cfg.ranking.bm25_weight,
        embedding_weight=cfg.ranking.embedding_weight,
        embedding_model=cfg.ranking.embedding_model,
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
        path = Path(s.path)
        exists = path.exists()
        n_docs = sum(1 for _ in discover_documents(s)) if exists else 0
        out.append(
            {
                "name": s.name,
                "path": s.path,
                "frontmatter": s.frontmatter,
                "exists": exists,
                "documents": n_docs,
            }
        )
    typer.echo(json.dumps(out, indent=2))


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
            if not Path(s.path).exists():
                issues.append(f"Source '{s.name}' path missing: {s.path}")
    except Exception as e:
        issues.append(f"Failed to load config: {e}")

    # Optional deps
    if importlib.util.find_spec("sentence_transformers") is None:
        notes.append("sentence-transformers: not installed (BM25-only retrieval)")
    else:
        notes.append("sentence-transformers: installed")
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


def main():
    app()


if __name__ == "__main__":
    main()
