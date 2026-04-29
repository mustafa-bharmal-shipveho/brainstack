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


def main():
    app()


if __name__ == "__main__":
    main()
