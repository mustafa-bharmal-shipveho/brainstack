"""recall CLI surface.

Subcommands: query, reindex, sources, doctor.

Init/migrate are stubs in this release — the safety design lives in
recall.migrate but the CLI wiring is deferred until the retriever's ranking
quality is validated on real prompts.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import NoReturn, Optional

import typer

from recall import __version__
from recall.config import (
    Config,
    cache_dir,
    config_path,
    load_config,
    resolve_brain_home,
)
from recall.config import effective_mode as _effective_mode
from recall.core import HybridRetriever
from recall.index import build_index, load_index, needs_refresh
from recall.qdrant_backend import (
    QdrantStoreAccessError,
    QdrantStoreBusyError,
    close_client_cache,
)
from recall.serialize import serialize_results, wire_to_serialized
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


def _load_or_build(cfg: Config, mode: str = "hybrid") -> tuple[Optional[object], bool]:
    """Returns (cache, fresh).

    `fresh=True` means we just rebuilt — the in-memory documents list is
    authoritative. `fresh=False` means the existing Qdrant collection is
    valid; the documents list is reconstructed from disk for shape-compat
    but the retriever should query the existing collection directly.

    `mode` is forwarded to the build so a `sparse` query against a cold or
    stale index does not trigger a dense-model download during indexing.
    """
    if needs_refresh(cfg.sources):
        return build_index(cfg.sources, mode=mode), True
    return load_index(cfg.sources), False


_serialize = serialize_results  # backwards-compat alias inside the module


def _exit_qdrant_store_error(
    exc: QdrantStoreAccessError | QdrantStoreBusyError,
) -> NoReturn:
    message = str(exc)
    # "index is busy" is almost always the warm daemon holding the store's
    # exclusive process lock. Without naming it, the user has nothing
    # visibly installed to explain the failure.
    sock = _daemon_status_socket()
    if sock is not None:
        message += (
            f"\nA recall daemon is running at {sock} and owns this store. "
            "`recall query` and `recall reindex` route through it "
            "automatically; stop it with `recall serve --stop` if you need "
            "direct access."
        )
    typer.echo(message, err=True)
    raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Warm daemon routing (S3)
#
# While `recall serve` runs it OWNS the embedded Qdrant store, so the CLI
# talks to the socket instead of racing it for the exclusive process lock.
# ---------------------------------------------------------------------------


def _resolve_daemon_socket() -> Optional[Path]:
    """The socket the CLI should talk to, or None if it cannot be resolved.

    Resolves through `recall.config`, NOT `recall.daemon`: importing the
    daemon module pulls `recall.index` -> `qdrant_client` (~0.9 s) just to
    join a few path components, and `recall doctor` / `recall serve
    --status` pay that on every call. Resolution is a config concern; the
    daemon module is the server.
    """
    try:
        from recall.config import daemon_socket_path

        return Path(daemon_socket_path())
    except Exception:  # noqa: BLE001 - the daemon is a soft dependency
        return None


def _daemon_disabled() -> bool:
    return os.environ.get("RECALL_NO_DAEMON") == "1"


def _daemon_status_socket() -> Optional[Path]:
    """Return the socket path IF a daemon is answering on it, else None."""
    sock = _resolve_daemon_socket()
    if sock is None:
        return None
    try:
        from recall import daemon_client

        return sock if daemon_client.status(sock, timeout_s=0.5) is not None else None
    except Exception:  # noqa: BLE001
        return None


def _daemon_cli_budget_ms() -> int:
    """How long the CLI waits on the daemon. Generous compared with the
    hook's 800 ms: a cold daemon's first query pays the embedder load (and
    the cross-encoder too, under `--rerank`), and an interactive command can
    afford to wait for that instead of silently producing worse results."""
    raw = os.environ.get("RECALL_CLI_DAEMON_BUDGET_MS")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return 60_000


def _query_via_daemon(
    prompt: str,
    *,
    k: int,
    source_filter: Optional[str],
    type_filter: Optional[str],
    rerank: Optional[bool],
) -> Optional[list[dict]]:
    """Answer a query through the daemon.

    Returns the projected rows, or None when the daemon is DOWN (nothing
    holds the store lock, so the direct path is safe). When the daemon is
    UP but unhealthy the direct path would only block on fcntl until it
    timed out, so that case exits with an actionable message instead.
    """
    if _daemon_disabled():
        return None
    sock = _resolve_daemon_socket()
    if sock is None:
        return None
    # No ImportError guard: `daemon_client` is stdlib-only by design (see
    # its module docstring), so it cannot fail to import where `recall`
    # itself imported.
    from recall import daemon_client

    try:
        resp = daemon_client.query(
            prompt,
            k=k,
            socket_path=sock,
            budget_ms=_daemon_cli_budget_ms(),
            source_filter=source_filter,
            type_filter=type_filter,
            rerank=rerank,
        )
    except daemon_client.DaemonUnavailable as exc:
        if exc.reason in daemon_client.DAEMON_DOWN_REASONS:
            return None
        typer.echo(
            f"recall query: the daemon at {sock} is running but did not answer "
            f"({exc.reason}: {exc}). It owns the index, so the direct path "
            f"would just block on the store lock. Check `recall serve "
            f"--status`, retry, or stop it with `recall serve --stop`.",
            err=True,
        )
        raise typer.Exit(code=1) from exc
    return [wire_to_serialized(item) for item in (resp.get("results") or [])]


def _query_results(
    retriever: HybridRetriever,
    query: str,
    *,
    k: int,
    strategy: str,
    type_filter: Optional[str] = None,
    source_filter: Optional[str] = None,
):
    if strategy == "ranked":
        return retriever.query(
            query,
            k=k,
            type_filter=type_filter,
            source_filter=source_filter,
        )
    if strategy == "context":
        return retriever.query_context(
            query,
            k=k,
            type_filter=type_filter,
            source_filter=source_filter,
        )
    raise ValueError('strategy must be "ranked" or "context"')


def _expanded_query(
    retriever: HybridRetriever,
    query: str,
    *,
    k: int,
    expand_n: int,
    strategy: str,
    type_filter: Optional[str] = None,
    source_filter: Optional[str] = None,
    rerank_model: Optional[str] = None,
    per_variant_k: int = 10,
    rerank_cap: int = 20,
):
    """Query expansion + RRF fusion + optional post-hoc rerank on union.

    Empirical sweet spot (real-brain hard queries, see
    tests/recall/eval_expansion.py):

      - per_variant_k=10: enough to surface the right doc most of the time
        without diluting the reranker with low-quality candidates
      - 4 variants total (original + expand_n=3)
      - RRF (k=60) merge across variants
      - Post-hoc rerank with cross-encoder if rerank is enabled

    When `rerank_model` is None (rerank=off), the union is returned by
    RRF score ordering. When set, the cross-encoder rescores up to
    `rerank_cap` of the top-ranked union members against the ORIGINAL query
    and we return its top-k. Capping matters on the jina-v2 path where
    reranking the whole 40-doc union pushes p50 from ~1s to 30s on CPU.
    """
    from recall.expand import expand_query
    from recall.fusion import rrf_merge

    typer.echo(f"expanding via {_expand_provider_label()}...", err=True)
    variants = expand_query(query, n=expand_n)
    per_variant = [
        _query_results(
            retriever,
            v,
            k=per_variant_k,
            strategy=strategy,
            type_filter=type_filter,
            source_filter=source_filter,
        )
        for v in variants
    ]
    # Pin the original query's top-1 to position 0 — without this, RRF
    # can demote the doc that's-most-relevant-to-the-actual-query in favor
    # of a doc-that's-merely-frequent-across-paraphrases, which on the
    # maintainer's hard golden set dropped Recall@1 from 13.2% to 5.3%.
    fused = rrf_merge(per_variant, pin_first_variant_top=True)

    if rerank_model is None or not fused:
        return fused[:k]

    # Post-hoc rerank against the ORIGINAL query (not the paraphrases) —
    # the user asked the original, that's what we judge against.
    #
    # Truncate the union to `rerank_cap` candidates first. With 4 variants
    # x per_variant_k=10 the union is up to ~40 docs; cross-encoders scale
    # linearly with the candidate count, and the jina-v2 path takes ~30s
    # for the full 40 on CPU. The RRF-top 20 capture the right doc in
    # practice (empirically Recall@10 = 71% for the un-reranked top-10).
    from recall import qdrant_backend
    from recall.core import QueryResult

    candidates = fused[:rerank_cap]
    encoder = qdrant_backend._get_cross_encoder(rerank_model)
    texts = [qr.document.text for qr in candidates]
    scores = list(encoder.rerank(query, texts))
    reranked = sorted(zip(candidates, scores), key=lambda pair: pair[1], reverse=True)
    rescored = [
        QueryResult(document=qr.document, score=float(s)) for qr, s in reranked
    ]
    # The per-variant results had the retriever's needs_review policy
    # applied, but the cross-encoder just replaced those scores with raw
    # relevance, losing the demotion. Re-apply the policy on the fused,
    # reranked union (before truncation, so a dropped/demoted flagged doc
    # frees its slot) so flagged memories stay demoted/excluded.
    from recall.core import apply_review_policy

    rescored = apply_review_policy(
        rescored,
        getattr(retriever, "_needs_review_policy", "ignore"),
        float(getattr(retriever, "_needs_review_penalty", 0.5)),
    )
    return rescored[:k]


def _expand_provider_label() -> str:
    """Best-effort LLM provider name for the expansion progress line.

    Mirrors resolve_provider precedence cheaply (env var first) and never
    raises; the label is cosmetic, expansion itself fails open anyway.
    """
    label = os.environ.get("BRAIN_LLM_PROVIDER")
    if label:
        return label
    try:
        from agent.tools.llm_providers import resolve_provider

        return resolve_provider(None).name or "llm"
    except Exception:
        return "llm"


class _StrategyRetriever:
    def __init__(self, retriever: HybridRetriever, strategy: str):
        self._retriever = retriever
        self._strategy = strategy

    def query(
        self,
        query: str,
        k: int,
        type_filter: Optional[str] = None,
        source_filter: Optional[str] = None,
    ):
        return _query_results(
            self._retriever,
            query,
            k=k,
            strategy=self._strategy,
            type_filter=type_filter,
            source_filter=source_filter,
        )


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
    rerank_model: str = typer.Option(
        None,
        "--rerank-model",
        help=(
            "Override the cross-encoder model (requires --rerank cross_encoder). "
            "Examples: jinaai/jina-reranker-v1-turbo-en (small, fast, default), "
            "BAAI/bge-reranker-base (1GB, +9pp NDCG vs default), "
            "jinaai/jina-reranker-v2-base-multilingual (1.1GB, +17pp NDCG, 30s on CPU)."
        ),
    ),
    strategy: str = typer.Option(
        "ranked",
        "--strategy",
        help='Retrieval strategy: "ranked" for exact lookup, "context" for broad LLM prompts.',
    ),
    mode: str = typer.Option(
        None,
        "--mode",
        help=(
            'Retrieval mode: "hybrid" (dense + sparse, best quality), '
            '"dense", or "sparse" (BM25-only; works before the embedding '
            "model is downloaded). Precedence: this flag > RECALL_MODE env "
            "var > config ranking.mode."
        ),
    ),
    expand: Optional[bool] = typer.Option(
        None,
        "--expand/--no-expand",
        help=(
            "LLM-generate query paraphrases, retrieve each, RRF-fuse with "
            "the original query's top-1 pinned (so Recall@1 floors at the "
            "no-expand baseline), and (if --rerank is on) post-rerank the "
            "union against the original query. Lift on real-brain hard "
            "queries: +18pp Recall@5, +24pp Recall@10, with no Recall@1 "
            "regression. Costs one LLM CLI round-trip per query, measured "
            "~5-20 s with a cold claude/codex CLI; worth it for hard "
            "semantic queries. Default: off unless ranking.expand_default "
            "is true in config."
        ),
    ),
    expand_n: int = typer.Option(
        3,
        "--expand-n",
        help="Number of paraphrases to generate when --expand is on.",
    ),
    no_daemon: bool = typer.Option(
        False,
        "--no-daemon",
        help="Force the in-process retrieval path even if a `recall serve` "
             "daemon is running (debugging, or a wedged daemon). Note the "
             "daemon owns the index while it runs, so the direct path can "
             "report 'index is busy'.",
    ),
):
    """Search the brain for memories relevant to QUERY. Outputs JSON."""
    try:
        if strategy not in {"ranked", "context"}:
            typer.echo('recall query: --strategy must be "ranked" or "context"', err=True)
            raise typer.Exit(code=2)
        cfg = load_config()
        # Mode precedence: flag > RECALL_MODE env > config.
        effective_mode = _effective_mode(cfg, mode)
        if effective_mode not in {"hybrid", "dense", "sparse"}:
            typer.echo(
                'recall query: mode must be "hybrid", "dense", or "sparse" '
                f"(got {effective_mode!r})",
                err=True,
            )
            raise typer.Exit(code=2)

        query_str = " ".join(text)
        effective_k = k if k is not None else cfg.default_k
        # Tri-state expand flag: explicit --expand/--no-expand wins; when
        # neither is passed, fall back to the config default (off unless
        # the user opted in via ranking.expand_default).
        effective_expand = expand if expand is not None else cfg.ranking.expand_default

        # Daemon-first, BEFORE `_load_or_build` — opening the store here
        # while a daemon holds its exclusive lock is exactly the "index is
        # busy" failure the routing exists to avoid. Only the plain ranked
        # path is routable: query expansion, a per-call rerank model, and an
        # explicit --mode all need retriever state the daemon does not have.
        routable = (
            not no_daemon
            and mode is None
            and strategy == "ranked"
            and not effective_expand
            and rerank_model is None
            and rerank in (None, "cross_encoder", "none")
        )
        if routable:
            routed = _query_via_daemon(
                query_str,
                k=effective_k,
                source_filter=source,
                type_filter=type,
                rerank=None if rerank is None else (rerank == "cross_encoder"),
            )
            if routed is not None:
                typer.echo(json.dumps(routed, indent=2))
                raise typer.Exit(code=0)

        cache, fresh = _load_or_build(cfg, mode=effective_mode)
        if cache is None or not cache.documents:
            typer.echo("[]")
            raise typer.Exit(code=0)

        effective_reranker = rerank if rerank is not None else cfg.ranking.reranker
        effective_rerank_model = rerank_model if rerank_model is not None else cfg.ranking.reranker_model
        retriever = HybridRetriever(
            documents=cache.documents if fresh else None,
            collections=[s.name for s in cfg.sources],
            embedder=cfg.ranking.embedder,
            sparse_embedder=cfg.ranking.sparse_embedder,
            reranker=effective_reranker,
            reranker_model=effective_rerank_model,
            rerank_n=cfg.ranking.rerank_n,
            needs_review_policy=cfg.ranking.needs_review_policy,
            needs_review_penalty=cfg.ranking.needs_review_penalty,
            mode=effective_mode,
        )

        if effective_expand:
            results = _expanded_query(
                retriever,
                query_str,
                k=effective_k,
                expand_n=expand_n,
                strategy=strategy,
                type_filter=type,
                source_filter=source,
                rerank_model=effective_rerank_model if effective_reranker == "cross_encoder" else None,
            )
        else:
            results = _query_results(
                retriever,
                query_str,
                k=effective_k,
                strategy=strategy,
                type_filter=type,
                source_filter=source,
            )
        typer.echo(json.dumps(_serialize(results), indent=2))
    except (QdrantStoreAccessError, QdrantStoreBusyError) as exc:
        _exit_qdrant_store_error(exc)
    finally:
        close_client_cache()


@app.command()
def reindex():
    """Rebuild the index cache from scratch."""
    # Daemon-first: while `recall serve` runs it holds the store's exclusive
    # process lock, so building here would just fail with "index is busy".
    # The daemon's `reindex` op runs the same chunked pass synchronously.
    if not _daemon_disabled():
        sock = _resolve_daemon_socket()
        if sock is not None:
            from recall import daemon_client

            try:
                resp = daemon_client.reindex(sock)
            except daemon_client.DaemonUnavailable as exc:
                if exc.reason not in daemon_client.DAEMON_DOWN_REASONS:
                    typer.echo(
                        f"recall reindex: the daemon at {sock} is running but "
                        f"the refresh failed ({exc.reason}: {exc}). It owns the "
                        f"index; check `recall serve --status` or stop it with "
                        f"`recall serve --stop`.",
                        err=True,
                    )
                    raise typer.Exit(code=1) from exc
            else:
                typer.echo(
                    f"Reindexed via the daemon at {sock}: "
                    f"{resp.get('changed', 0)} document(s) re-embedded, "
                    f"{resp.get('deleted', 0)} stale point(s) removed "
                    f"({resp.get('ms', 0)} ms)."
                )
                return

    try:
        cfg = load_config()
        cache = build_index(cfg.sources)
        typer.echo(
            f"Indexed {len(cache.documents)} documents across {len(cfg.sources)} source(s)."
        )
    except (QdrantStoreAccessError, QdrantStoreBusyError) as exc:
        _exit_qdrant_store_error(exc)
    finally:
        close_client_cache()


@app.command()
def serve(
    socket: Optional[Path] = typer.Option(
        None, "--socket",
        help="AF_UNIX socket path (default: recall.config.daemon_socket_path()).",
    ),
    rerank: bool = typer.Option(
        False, "--rerank/--no-rerank",
        help="Load the cross-encoder reranker in the daemon process. OFF by "
             "default: on the calibrated set it produced no measurable "
             "ordering gain for 4-13x the latency (0.47-1.4 s vs a "
             "60-130 ms retrieval-only warm path), which does not fit the "
             "auto-recall hook's budget.",
    ),
    reranker_model: Optional[str] = typer.Option(
        None, "--reranker-model",
        help="Cross-encoder to load with --rerank. Defaults to the "
             "calibrated Xenova/ms-marco-MiniLM-L-6-v2 unless "
             "ranking.reranker_model is set in your config.",
    ),
    rerank_n: Optional[int] = typer.Option(
        None, "--rerank-n",
        help="Candidates fed to the cross-encoder with --rerank. Defaults "
             "to the calibrated 10 unless ranking.rerank_n is set in your "
             "config.",
    ),
    idle_timeout_s: float = typer.Option(
        0.0, "--idle-timeout-s",
        help="Exit after this many idle seconds with no requests (0 = never).",
    ),
    refresh_interval_s: float = typer.Option(
        300.0, "--refresh-interval-s",
        help="Background index-freshness pass interval, in seconds.",
    ),
    status: bool = typer.Option(
        False, "--status",
        help="Scriptable probe: print daemon status and exit 0 if running, "
             "1 if not (does not start a daemon).",
    ),
    stop: bool = typer.Option(
        False, "--stop",
        help="Shut down a running daemon and exit (does not start one).",
    ),
    json_out: bool = typer.Option(
        False, "--json",
        help="With --status, emit the status payload as JSON.",
    ),
):
    """Run the warm retrieval daemon: one long-lived `HybridRetriever`
    serving NDJSON requests over an AF_UNIX socket, so `recall query` and
    the auto-recall hook skip the cold-start + Qdrant store-lock cost on
    every call. The cross-encoder is NOT loaded unless you pass `--rerank`
    (it measured no ordering gain for 4-13x the latency).

    While a daemon owns the store, anything that opens it directly (a
    second `recall serve`, a bare `recall query` without daemon routing)
    gets "index is busy" — `recall query` routes through the socket
    instead of racing it (S3); `--no-daemon` on `query` is the escape
    hatch.
    """
    import json as _json

    # `recall.config`, not `recall.daemon`: `--status` and `--stop` are
    # probes, and must not pay the ~0.9 s qdrant import chain (nor fail
    # outright when qdrant is not importable) to find a socket path.
    from recall import daemon_client
    from recall.config import daemon_socket_path

    sock = Path(socket) if socket else Path(daemon_socket_path())

    if status:
        result = daemon_client.status(sock)
        if result is None:
            typer.echo(f"recall serve: not running at {sock}")
            raise typer.Exit(code=1)
        if json_out:
            typer.echo(_json.dumps(result, indent=2))
        else:
            typer.echo(f"recall serve: running (pid {result.get('pid')}) at {sock}")
        return

    if stop:
        ok = daemon_client.shutdown(sock)
        if ok:
            typer.echo(f"recall serve: stopped daemon at {sock}")
        else:
            typer.echo(f"recall serve: no daemon running at {sock}", err=True)
            raise typer.Exit(code=1)
        return

    from recall.daemon import run_daemon

    exit_code = run_daemon(
        socket_path=sock,
        rerank=rerank,
        reranker_model=reranker_model,
        rerank_n=rerank_n,
        idle_timeout_s=idle_timeout_s,
        refresh_interval_s=refresh_interval_s,
    )
    raise typer.Exit(code=exit_code or 0)


@app.command("eval")
def eval_command(
    cases_file: Path = typer.Argument(..., help="JSONL eval cases."),
    k: int = typer.Option(5, "--k", "-k", help="Number of retrieval results per query."),
    details: Optional[Path] = typer.Option(
        None,
        "--details",
        help="Optional path to write per-case JSONL results.",
    ),
    human: bool = typer.Option(
        False,
        "--human",
        help="Print a human-readable report instead of JSON.",
    ),
    rerank: str = typer.Option(
        None,
        "--rerank",
        help='Override reranker: "cross_encoder" | "none". Default = config value.',
    ),
    strategy: str = typer.Option(
        "ranked",
        "--strategy",
        help='Retrieval strategy under test: "ranked" or "context".',
    ),
):
    """Evaluate retrieval against a private JSONL query set."""
    try:
        from recall.eval import load_eval_cases, render_human, run_eval, write_details

        if k < 5:
            typer.echo("recall eval: --k must be >= 5", err=True)
            raise typer.Exit(code=2)
        if strategy not in {"ranked", "context"}:
            typer.echo('recall eval: --strategy must be "ranked" or "context"', err=True)
            raise typer.Exit(code=2)
        cases = load_eval_cases(cases_file)
        cfg = load_config()
        cache, fresh = _load_or_build(cfg)
        if cache is None or not cache.documents:
            typer.echo("recall eval: index has no documents; run `recall reindex`", err=True)
            raise typer.Exit(code=1)

        effective_reranker = rerank if rerank is not None else cfg.ranking.reranker
        retriever = HybridRetriever(
            documents=cache.documents if fresh else None,
            collections=[s.name for s in cfg.sources],
            embedder=cfg.ranking.embedder,
            sparse_embedder=cfg.ranking.sparse_embedder,
            reranker=effective_reranker,
            reranker_model=cfg.ranking.reranker_model,
            rerank_n=max(cfg.ranking.rerank_n, k),
            needs_review_policy=cfg.ranking.needs_review_policy,
            needs_review_penalty=cfg.ranking.needs_review_penalty,
        )
        report = run_eval(_StrategyRetriever(retriever, strategy), cases, k=k)
        if details is not None:
            write_details(details, report.results)
        if human:
            typer.echo(render_human(report))
        else:
            typer.echo(json.dumps(report.to_dict(), indent=2))
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    except (QdrantStoreAccessError, QdrantStoreBusyError) as exc:
        _exit_qdrant_store_error(exc)
    finally:
        close_client_cache()


@app.command()
def lint(
    stale: bool = typer.Option(
        False, "--stale",
        help="Only run staleness checks (dead paths, broken links). "
             "Default: all checks including frontmatter integrity.",
    ),
    mark: bool = typer.Option(
        False, "--mark",
        help="Reconcile `needs_review` flags with reality: ADD the flag to "
             "memories that have findings, and AUTO-CLEAR it from memories "
             "that carry the flag but now have none (e.g. a dead path was "
             "recreated). Off by default — plain lint is report-only.",
    ),
    no_clear: bool = typer.Option(
        False, "--no-clear",
        help="With --mark, only ADD flags; do not auto-clear flags from "
             "now-fresh memories (use if you hand-set needs_review yourself).",
    ),
    repair: bool = typer.Option(
        False, "--repair",
        help="Find memories whose frontmatter YAML doesn't parse (usually an "
             "unquoted value with a colon) and PREVIEW the fix (re-quote the "
             "offending value). Preview-only by default; add --apply to write. "
             "Body preserved exactly; only writes if the result parses.",
    ),
    apply: bool = typer.Option(
        False, "--apply",
        help="With --repair, actually write the repairs (default is a dry-run "
             "preview, since repair mutates your memory files in place).",
    ),
    json_out: bool = typer.Option(
        False, "--json",
        help="Emit findings as JSON instead of the human-readable report. "
             "On its own this is a bare array of findings. Combined with "
             "--fix-digests and/or --dedupe-claims it is a single object: "
             '{"findings": [...], "fix_digests": {...}|null, '
             '"dedupe_claims": {...}|null, "stub_min_chars": N} — every key '
             "is always present, and a surface you did not ask for is null. "
             "Either way stdout is exactly ONE JSON document, so it pipes "
             "straight into jq.",
    ),
    brain: Optional[Path] = typer.Option(
        None, "--brain",
        help="Memory root to lint (default: resolved brain memory dir).",
    ),
    fix_digests: bool = typer.Option(
        False, "--fix-digests",
        help="Backfill missing name/description/type frontmatter keys onto "
             "session digests (memory/semantic/digests/*.md). Splices into "
             "the existing frontmatter block; never rewrites the body. "
             "Dry-run by default; add --apply to write.",
    ),
    dedupe_claims: bool = typer.Option(
        False, "--dedupe-claims",
        help="Archive duplicate/stub claims (memory/semantic/claims/*.md) to "
             "semantic/archived/ and retract them in claim_overrides.jsonl so "
             "the next consolidation doesn't recreate them. Dry-run by "
             "default; add --apply to write.",
    ),
    stub_min_chars: int = typer.Option(
        0, "--stub-min-chars",
        help="With --dedupe-claims, also flag claim bodies shorter than N "
             "chars as stubs. Off (0) by default: claims are one-liners by "
             "construction, so length alone is not a content-free signal.",
    ),
):
    """Find stale or broken memories (deterministic, offline, high-precision).

    Scans every markdown memory and reports checkable problems:

      - dead_path: a backticked/linked absolute or ~/ path that no longer
        exists on disk (the headline staleness signal)
      - broken_wikilink: a [[target]] pointing at no memory
      - broken_local_link: a [text](path) to a missing local file
      - missing_frontmatter: a lesson missing name/description/type

    A bad injected memory is worse than a missing one, so checks are
    conservative — repo-relative paths, bare prose paths, and URL liveness
    are intentionally NOT checked to keep false positives near zero.

    Exit code is 0 when clean, 1 when any finding is reported.
    """
    import json as _json

    from recall.lint import (
        ALL_KINDS,
        STALE_KINDS,
        find_flagged_files,
        lint_brain,
        mark_needs_review,
        repair_frontmatter,
        unmark_needs_review,
    )

    brain_root = brain.expanduser() if brain else resolve_brain_home()
    if not brain_root.is_dir():
        typer.echo(f"recall lint: brain not found: {brain_root}", err=True)
        raise typer.Exit(code=2)

    kinds = STALE_KINDS if stale else ALL_KINDS
    findings = lint_brain(brain_root, kinds=kinds)

    # stdout must be ONE JSON document. When a manifest surface is also
    # requested, the findings array becomes a key of a single object emitted
    # once, after every manifest is computed — echoing it here as well would
    # put two top-level documents on stdout and break `| jq`.
    single_json = json_out and (fix_digests or dedupe_claims)

    if json_out:
        if not single_json:
            typer.echo(_json.dumps([f.to_dict() for f in findings], indent=2))
    else:
        from collections import Counter

        n_files = sum(1 for _ in brain_root.rglob("*.md"))
        typer.echo(f"== recall lint ==  ({n_files} memories scanned under {brain_root})")
        if not findings:
            typer.echo("No issues detected.")
        else:
            by_kind: Counter[str] = Counter(f.kind for f in findings)
            affected = len({f.file for f in findings})
            for f in findings:
                rel = f.file
                try:
                    rel = f.file.relative_to(brain_root)
                except ValueError:
                    pass
                typer.echo(f"  {f.severity:9s} {f.kind:18s} {rel}:{f.line}")
                typer.echo(f"            {f.detail}")
            typer.echo("")
            typer.echo(
                f"{len(findings)} finding(s) across {affected} file(s): "
                + ", ".join(f"{k}={v}" for k, v in by_kind.most_common())
            )

    if repair:
        detect = findings if kinds == ALL_KINDS else lint_brain(brain_root, kinds=ALL_KINDS)
        targets = {f.file for f in detect if f.kind == "unparseable_frontmatter"}
        if not apply:
            # Dry-run preview: repair mutates the user's only copy of their
            # notes, so never write without an explicit --apply.
            fixable = repair_frontmatter(targets, dry_run=True)
            for p in sorted(fixable):
                try:
                    rel = p.relative_to(brain_root)
                except ValueError:
                    rel = p
                typer.echo(f"  would repair  {rel}", err=True)
            typer.echo(
                f"\n{len(fixable)} file(s) repairable, "
                f"{len(targets) - len(fixable)} need manual review. "
                f"Re-run with --repair --apply to write.", err=True,
            )
        else:
            fixed = repair_frontmatter(targets)
            if fixed:
                typer.echo(f"\nRepaired frontmatter on {len(fixed)} file(s).", err=True)
            unfixable = len(targets) - len(fixed)
            if unfixable:
                typer.echo(
                    f"{unfixable} file(s) have unparseable frontmatter the repair "
                    f"couldn't fix automatically — review by hand.", err=True,
                )

    # --fix-digests / --dedupe-claims: independent surfaces from the
    # dead-path/wikilink checks above. Both dry-run by default (--apply
    # required to write). Under --json each folds its manifest into the
    # single object emitted below rather than echoing its own document.
    digest_manifest: Optional[dict] = None
    claim_manifest: Optional[dict] = None

    pending_digest_fixes: list = []
    if fix_digests:
        from recall.lint_digests import (
            apply_digest_fixes,
            plan_digest_fixes,
            render_digest_manifest,
        )

        fixes = plan_digest_fixes(brain_root)
        applied_digests = apply_digest_fixes(fixes, dry_run=False) if apply else None
        if json_out:
            digest_manifest = {
                "applied": applied_digests is not None,
                "fixes": [
                    {
                        "file": str(f.file),
                        "missing": list(f.missing),
                        "proposed": f.proposed,
                        "skipped_reason": f.skipped_reason,
                    }
                    for f in fixes
                ],
            }
        else:
            typer.echo(render_digest_manifest(fixes, brain_root, applied=applied_digests))
        pending_digest_fixes = (
            plan_digest_fixes(brain_root) if apply else fixes
        )

    pending_claim_actions: list = []
    if dedupe_claims:
        from recall.lint_claims import (
            apply_claim_dedupe,
            plan_claim_dedupe,
            render_claim_manifest,
        )

        actions = plan_claim_dedupe(brain_root, stub_min_chars=stub_min_chars)
        applied_claims = (
            apply_claim_dedupe(actions, brain_root, dry_run=False) if apply else None
        )
        if json_out:
            claim_manifest = {
                "applied": applied_claims is not None,
                "actions": [
                    {
                        "file": str(a.file),
                        "claim_id": a.claim_id,
                        "source_event_id": a.source_event_id,
                        "reason": a.reason,
                        "keep": str(a.keep) if a.keep else None,
                        "detail": a.detail,
                    }
                    for a in actions
                ],
            }
        else:
            typer.echo(render_claim_manifest(actions, brain_root, applied=applied_claims, stub_min_chars=stub_min_chars))
        pending_claim_actions = (
            plan_claim_dedupe(brain_root, stub_min_chars=stub_min_chars)
            if apply else actions
        )

    if single_json:
        typer.echo(_json.dumps(
            {
                "findings": [f.to_dict() for f in findings],
                "fix_digests": digest_manifest,
                "dedupe_claims": claim_manifest,
                "stub_min_chars": stub_min_chars,
            },
            indent=2,
        ))

    if mark:
        # Reconcile flags against the FULL check suite, independent of the
        # --stale display filter: needs_review is a single per-file boolean
        # about overall freshness, so a file with any finding stays flagged.
        # Recompute after a repair so the reconcile reflects the fixed state.
        full_findings = (
            lint_brain(brain_root, kinds=ALL_KINDS)
            if (repair or kinds != ALL_KINDS)
            else findings
        )
        files_with_issues = {f.file for f in full_findings}

        marked = mark_needs_review(files_with_issues)
        if marked:
            typer.echo(f"\nMarked needs_review on {len(marked)} file(s).", err=True)

        if not no_clear:
            # Auto-clear: any memory that carries the flag but no longer has a
            # finding (e.g. its dead path was recreated) gets un-flagged.
            stale_flagged = set(find_flagged_files(brain_root)) - files_with_issues
            cleared = unmark_needs_review(stale_flagged)
            if cleared:
                typer.echo(f"Auto-cleared needs_review on {len(cleared)} now-fresh file(s).", err=True)

    # Exit code reflects RESIDUAL findings. After --repair the brain may be
    # clean, so re-scan rather than trust the pre-repair snapshot (otherwise a
    # CI `recall lint --repair` that fixes everything would still fail).
    residual = lint_brain(brain_root, kinds=kinds) if repair else findings
    residual_digest_proposals = any(f.proposed for f in pending_digest_fixes)
    if residual or residual_digest_proposals or pending_claim_actions:
        raise typer.Exit(code=1)


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
    reviewed: bool = typer.Option(
        False, "--reviewed",
        help="write a durable, human-reviewed lesson immediately instead of "
             "staging it for review. Only pass this when a human made the call.",
    ),
    non_interactive_ack: bool = typer.Option(
        False, "--non-interactive-ack",
        help="acknowledge a durable --reviewed write from a non-interactive "
             "context (stdin is not a TTY). Without it, --reviewed is refused "
             "off a TTY so an agent, hook, or injected prompt cannot silently "
             "write durable memory.",
    ),
    brain_root: Path = typer.Option(
        Path("~/.agent").expanduser(),
        "--brain-root",
        help="brainstack memory root. Default: ~/.agent",
    ),
):
    """Remember a lesson: staged for review by default, durable with --reviewed.

    Writes a markdown file to ~/.agent/memory/semantic/lessons/<slug>.md
    with frontmatter. Different from `recall runtime add` which is
    session-scoped (one prompt only).

    By default the lesson carries `needs_review: true` so retrieval demotes
    (or excludes) it until a human accepts it via `recall pending --review`.
    `--reviewed` skips staging; it asserts a human approved this write. Off a
    TTY, `--reviewed` requires `--non-interactive-ack` (same human-decision
    gate as `graduate.py`).

    Examples:
        recall remember "always use /agent-team for development"
        recall remember "use SELECT FOR UPDATE SKIP LOCKED for queue claims" --as postgres-locking --reviewed
    """
    # Human-decision gate (mirrors agent/tools/graduate.py): a durable
    # --reviewed write must trace to a human. Off a TTY (hook/agent/pipe), an
    # injected `recall remember --reviewed` would otherwise bypass the review
    # staging the default path enforces. Require the explicit ack instead.
    if reviewed and not non_interactive_ack:
        try:
            is_tty = sys.stdin.isatty()
        except (AttributeError, ValueError):
            is_tty = False
        if not is_tty:
            typer.echo(
                "refusing a durable --reviewed write: stdin is not a TTY, so "
                "this looks like an agent, hook, or pipe rather than a human. "
                "Durable memory must be a human decision. Run from an "
                "interactive terminal, drop --reviewed to stage for review "
                "(recall pending --review), or, if a human already approved "
                "this exact lesson, re-run with --non-interactive-ack.",
                err=True,
            )
            raise typer.Exit(4)

    from recall.remember import write_lesson
    try:
        path = write_lesson(
            text=text,
            name=name or None,
            description=description or None,
            brain_root=brain_root,
            overwrite=overwrite,
            reviewed=reviewed,
        )
    except (FileNotFoundError, FileExistsError, ValueError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2)
    if reviewed:
        typer.echo(f"remembered: {path}")
        typer.echo("(brainstack will auto-load this lesson on every future session)")
    else:
        typer.echo(f"remembered (staged for review): {path}")
        typer.echo(
            "(retrieval demotes this lesson until a human accepts it; "
            "run `recall pending --review` to accept or reject)"
        )


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


def _install_root(brain: Path) -> Path:
    """Where the brainstack clone lives.

    install.sh pins it at <brain-root>/.brainstack-repo-path (one line,
    absolute path). Fall back to this package's own location for repo-side
    or pip-style installs where no pin exists.
    """
    pin = brain.parent / ".brainstack-repo-path"
    if pin.is_file():
        try:
            candidate = Path(pin.read_text(encoding="utf-8").strip()).expanduser()
            if candidate.is_dir():
                return candidate
        except OSError:
            pass
    return Path(__file__).resolve().parents[1]


def _dense_model_cached(cache: Path, model: str) -> bool:
    """Heuristic: fastembed stores weights under a directory whose name
    embeds the model name (hf-hub 'models--org--name' or the fast-* GCS
    layout), so a name-fragment match means the weights are on disk."""
    if not cache.is_dir():
        return False
    needle = model.split("/")[-1].lower()
    try:
        return any(needle in entry.name.lower() for entry in cache.iterdir())
    except OSError:
        return False


_ENV_ASSIGN_RE = __import__("re").compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _brainstack_hook_commands(settings_path: Path) -> list[str]:
    """Extract '# brainstack-runtime'-marked hook commands from Claude
    Code's settings.json. Returns [] when the file is absent/unparseable."""
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return []
    out: list[str] = []
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for h in entry.get("hooks") or []:
                if isinstance(h, dict):
                    cmd = h.get("command", "")
                    if isinstance(cmd, str) and "# brainstack-runtime" in cmd:
                        out.append(cmd)
    return out


def _parse_hook_command(cmd: str) -> tuple[Optional[str], Optional[str]]:
    """(interpreter, script) from an installed hook command.

    Shape: 'PYTHONPATH=<root> <python> <hooks.py> <Event>  # brainstack-runtime'.
    Leading VAR=value assignments are skipped; the first non-assignment
    token is the interpreter, the next is the hook script."""
    import shlex

    try:
        tokens = shlex.split(cmd, comments=True)
    except ValueError:
        return None, None
    interp: Optional[str] = None
    script: Optional[str] = None
    for tok in tokens:
        if interp is None:
            if _ENV_ASSIGN_RE.match(tok):
                continue
            interp = tok
        elif script is None:
            script = tok
        else:
            break
    return interp, script


def _check_hook_interpreters(notes: list[str], issues: list[str]) -> None:
    """Doctor sub-check: every installed brainstack hook must point at an
    interpreter that can actually import qdrant_client, or auto-recall
    silently no-ops on every prompt."""
    import subprocess

    home = Path(os.environ.get("HOME") or Path.home())
    commands = _brainstack_hook_commands(home / ".claude" / "settings.json")
    if not commands:
        notes.append("Auto-recall hooks: not installed in ~/.claude/settings.json")
        return
    probed: set[tuple[str, str]] = set()
    for cmd in commands:
        interp, script = _parse_hook_command(cmd)
        if interp is None:
            continue
        key = (interp, script or "")
        if key in probed:
            continue
        probed.add(key)
        missing = [p for p in (interp, script) if p and not Path(p).exists()]
        if missing:
            issues.append(
                f"Claude Code hook references missing path(s): {', '.join(missing)}. "
                "The hooks point into a missing brainstack clone (was it moved "
                "or deleted?). Re-run ./install.sh from the clone's current "
                "location."
            )
            continue
        try:
            probe = subprocess.run(
                [interp, "-c", "import qdrant_client"],
                capture_output=True,
                timeout=10,
            )
            ok = probe.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        if ok:
            notes.append(f"Auto-recall hook interpreter: {interp} (qdrant_client OK)")
        else:
            issues.append(
                f"auto-recall hook interpreter {interp} cannot import "
                "qdrant_client, so the hook silently no-ops on every prompt. "
                "Re-run './install.sh --enable-auto-recall' to repin the hook "
                "to an interpreter with the runtime deps."
            )


def _check_secret_scanner(brain: Path, notes: list[str], issues: list[str]) -> None:
    """Doctor sub-check: a brain that pushes to a git remote needs a secret
    scanner on PATH or sync.sh fails closed before every push."""
    import shutil
    import subprocess

    # `brain` is the memory home, which on a standard install is
    # <brain-root>/memory while the git repo + origin live at <brain-root>.
    # Check the resolved dir first, then its parent, so the scanner check
    # actually fires on the layout the installer creates (without this it
    # returned early and silently missed the failing-closed sync).
    git_root = None
    for cand in (brain, brain.parent):
        if (cand / ".git").exists():
            git_root = cand
            break
    if git_root is None:
        return
    brain = git_root
    try:
        remote = subprocess.run(
            ["git", "-C", str(brain), "remote", "get-url", "origin"],
            capture_output=True,
            timeout=10,
        )
        has_origin = remote.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        has_origin = False
    if not has_origin:
        notes.append("Brain git repo has no origin remote; secret-scanner check skipped")
        return
    if shutil.which("trufflehog") or shutil.which("gitleaks"):
        notes.append("Secret scanner: present (trufflehog/gitleaks on PATH)")
    else:
        issues.append(
            "brain has a git origin remote but neither trufflehog nor "
            "gitleaks is on PATH; sync.sh fails closed before push. Run "
            "'./install.sh --install-scanner' to install one."
        )


def _check_daemon(notes: list[str]) -> None:
    """Doctor sub-check: note whether the `recall serve` daemon is
    running. The daemon is a soft dependency (S3) — never let a probe
    failure (module not yet available, socket error, etc.) break the
    rest of doctor's report."""
    try:
        from recall import daemon_client
        from recall.config import daemon_socket_path

        sock = Path(daemon_socket_path())
        result = daemon_client.status(sock)
    except Exception:
        return
    if result is None:
        notes.append(
            f"Daemon: not running at {sock} — hooks use the slow in-process "
            "path; ./install.sh --setup-daemon"
        )
        return
    pid = result.get("pid")
    uptime_m = int((result.get("uptime_s") or 0) // 60)
    queries = result.get("queries_served")
    rerank = "on" if result.get("rerank") else "off"
    notes.append(
        f"Daemon: running (pid {pid}, up {uptime_m}m, {queries} queries, "
        f"rerank {rerank})"
    )


@app.command()
def health(
    json_out: bool = typer.Option(
        False, "--json",
        help="Emit the health report as JSON instead of the human-readable view.",
    ),
    brain_root: Optional[Path] = typer.Option(
        None, "--brain-root",
        help="Brain root to check (default: $BRAIN_ROOT or resolve_brain_home()).",
    ),
    cwd: Optional[Path] = typer.Option(
        None, "--cwd",
        help="Directory to resolve the auto-recall config from (default: os.getcwd()).",
    ),
    write: Optional[Path] = typer.Option(
        None, "--write",
        help="Also write the JSON report to this path (e.g. runtime/health.json).",
    ),
):
    """Run the brainstack health-check catalogue (drift, sync, log sizes,
    dream freshness, launch agents, daemon, auto-recall config, ...).

    Exit code is 1 when the overall status is FAIL, 0 for PASS/WARN/SKIP —
    a WARN is information, not a failure, so an hourly LaunchAgent running
    this doesn't record a spurious non-zero exit.
    """
    import json as _json

    from recall import health as _health

    env = _health.build_env(brain_root=brain_root, cwd=cwd)
    report = _health.run_health(env)
    if write is not None:
        _health.write_report(report, write)
    if json_out:
        typer.echo(_json.dumps(report.to_dict(), indent=2))
    else:
        typer.echo(report.render_human())
    if report.status == "FAIL":
        raise typer.Exit(code=1)


@app.command()
def doctor(
    health: bool = typer.Option(
        False, "--health",
        help="Append the `recall health` report and factor a FAIL into "
             "doctor's exit code (alias for users who only remember `doctor`).",
    ),
):
    """Diagnose configuration and dependency issues."""
    import tempfile

    from recall.qdrant_backend import _fastembed_cache_dir

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

    # Install root (the brainstack clone the hooks/tools resolve to)
    notes.append(f"Install root: {_install_root(brain)}")

    # Cache dir
    cd = cache_dir()
    notes.append(f"Cache dir: {cd}")

    # Sources
    cfg: Optional[Config] = None
    try:
        cfg = load_config()
        for s in cfg.sources:
            if not Path(s.resolved_path).exists():
                if s.path == s.resolved_path:
                    # No $VAR expansion happened — the configured path IS
                    # the resolved path. An arrow between two copies of the
                    # same string would falsely imply a mapping.
                    issues.append(f"Source '{s.name}' path missing: {s.path}")
                else:
                    issues.append(
                        f"Source '{s.name}' path missing: {s.path} → {s.resolved_path}"
                    )
    except Exception as e:
        issues.append(f"Failed to load config: {e}")

    # Required deps for retrieval (Qdrant + FastEmbed)
    try:
        from importlib.metadata import version as _ver

        import qdrant_client  # noqa: F401
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

    # FastEmbed model cache: where weights actually live + whether the
    # dense model is on disk yet.
    fe_cache = Path(_fastembed_cache_dir())
    dense_model = cfg.ranking.embedder if cfg is not None else "BAAI/bge-base-en-v1.5"
    dense_cached = _dense_model_cached(fe_cache, dense_model)
    notes.append(f"FastEmbed cache: {fe_cache}")
    notes.append(f"FastEmbed models cached: {'yes' if dense_cached else 'no'}")
    legacy_cache = Path(tempfile.gettempdir()) / "fastembed_cache"
    if legacy_cache.exists() and legacy_cache != fe_cache:
        notes.append(
            f"Legacy model cache present at {legacy_cache} (volatile $TMPDIR "
            f"location; superseded by {fe_cache}, safe to delete)"
        )
    if not dense_cached:
        notes.append(
            f"First reindex downloads {dense_model} (~440 MB) to {fe_cache} (one-time)."
        )

    # Effective retrieval mode (RECALL_MODE env > config ranking.mode), via
    # the shared resolver so doctor never disagrees with query/index/MCP.
    configured_mode = cfg.ranking.mode if cfg is not None else "hybrid"
    effective_mode = (
        _effective_mode(cfg)
        if cfg is not None
        else (os.environ.get("RECALL_MODE") or configured_mode)
    )
    if effective_mode == "hybrid" and not dense_cached:
        notes.append(
            "Retrieval mode: BM25-only fallback (dense model not cached); "
            "run 'recall reindex' to download it and restore hybrid retrieval"
        )
    else:
        notes.append(f"Retrieval mode: {effective_mode}")

    # Hook interpreter + secret scanner deep checks.
    _check_hook_interpreters(notes, issues)
    _check_secret_scanner(brain, notes, issues)
    _check_daemon(notes)

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
    else:
        typer.echo("\nNo issues detected.")

    # `--health`: append the `recall health` report. A health FAIL alone
    # is enough to exit 1, independent of doctor's own issues above.
    health_failed = False
    if health:
        from recall import health as _health

        health_report = _health.run_health(_health.build_env())
        typer.echo("")
        typer.echo(health_report.render_human())
        health_failed = health_report.status == "FAIL"

    if issues or health_failed:
        raise typer.Exit(code=1)


def _staged_remember_lessons(brain_root: Path) -> list[Path]:
    """Lessons staged by `recall remember`: frontmatter carries a truthy
    needs_review AND review_reason: unreviewed-remember. Regex scan of the
    raw frontmatter block (consistent with recall.lint detection) so
    malformed-YAML files don't hide their flag."""
    import re

    from recall.lint import _NEEDS_REVIEW_TRUE_RE, _read_frontmatter_bounds

    reason_re = re.compile(
        r"""(?mi)^review_reason[ \t]*:[ \t]*['"]?unreviewed-remember\b"""
    )
    staged: list[Path] = []
    lessons_dir = brain_root / "memory" / "semantic" / "lessons"
    if not lessons_dir.is_dir():
        return staged
    for md in sorted(lessons_dir.glob("*.md")):
        bounds = _read_frontmatter_bounds(md)
        if bounds is None:
            continue
        raw, _newline, body_start, fm_end = bounds
        fm_region = raw[body_start:fm_end]
        if _NEEDS_REVIEW_TRUE_RE.search(fm_region) and reason_re.search(fm_region):
            staged.append(md)
    return staged


def _accept_staged_lesson(path: Path) -> bool:
    """Promote one staged lesson to durable: drop the needs_review +
    review_reason staging keys, stamp reviewed_by: human-cli. Body bytes
    and newline style preserved; atomic write (same contract as
    recall.lint mark/unmark). Returns True if the file was modified."""
    import re

    from recall.lint import _atomic_write, _read_frontmatter_bounds, unmark_needs_review

    unmark_needs_review([path])
    bounds = _read_frontmatter_bounds(path)
    if bounds is None:
        return False
    raw, newline, body_start, fm_end = bounds
    fm_region = raw[body_start:fm_end]
    kept = [
        ln for ln in fm_region.split(newline)
        if not re.match(r"review_reason[ \t]*:", ln)
    ]
    if not any(re.match(r"reviewed_by[ \t]*:", ln) for ln in kept):
        kept.append("reviewed_by: human-cli")
    new_raw = raw[:body_start] + newline.join(kept) + raw[fm_end:]
    return _atomic_write(path, new_raw)


def _review_staged_lessons(brain_root: Path) -> None:
    """Interactive pre-pass over lessons staged by `recall remember`.

    Caller has already verified stdin is a TTY. Each staged lesson gets an
    [a]ccept / [r]eject / [s]kip prompt: accept promotes it to durable
    (reviewed_by: human-cli), reject archives it via the forget path so it
    stays recoverable, skip leaves it staged."""
    from recall.forget import archive_lesson

    staged = _staged_remember_lessons(brain_root)
    if not staged:
        return

    typer.echo(
        f"recall pending: {len(staged)} lesson(s) staged by `recall remember` "
        f"awaiting review"
    )
    for path in staged:
        typer.echo(f"\n--- {path.name} ---")
        try:
            typer.echo(path.read_text(encoding="utf-8").rstrip())
        except OSError as e:
            typer.echo(f"  (unreadable: {e}; skipping)", err=True)
            continue
        while True:
            try:
                choice = input("[a]ccept / [r]eject / [s]kip > ").strip().lower()
            except EOFError:
                typer.echo("(EOF; leaving remaining lessons staged)")
                return
            if choice in ("a", "accept"):
                if _accept_staged_lesson(path):
                    typer.echo(f"  accepted: {path.name} (reviewed_by: human-cli)")
                else:
                    typer.echo(f"  could not update {path.name}; left staged", err=True)
                break
            if choice in ("r", "reject"):
                result = archive_lesson(path.name, brain_root=brain_root)
                if result.archived_path:
                    typer.echo(f"  rejected: archived to {result.archived_path}")
                else:
                    typer.echo(f"  could not archive {path.name}; left staged", err=True)
                break
            if choice in ("s", "skip"):
                typer.echo("  skipped (still staged)")
                break
            typer.echo("  (a, r, or s)")


@app.command()
def pending(
    refresh: bool = typer.Option(
        False, "--refresh",
        help="Force-regenerate <brain>/PENDING_REVIEW.md before printing.",
    ),
    review: bool = typer.Option(
        False, "--review",
        help="Review staged `recall remember` lessons, then open the "
             "candidate triage flow. Requires an interactive terminal.",
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

    # Review mode: first an in-package pre-pass over lessons staged by
    # `recall remember` (needs_review + review_reason: unreviewed-remember),
    # then hand off to the interactive triage REPL for dream candidates.
    # Both stages are TTY-gated: acceptance or rejection of memory is a
    # USER decision. Driven without a TTY (i.e. by an agent), this branch
    # modifies nothing and never exec's the REPL.
    if review:
        if not sys.stdin.isatty():
            typer.echo(
                "recall pending --review requires an interactive terminal: "
                "accepting or rejecting staged memory is a human decision. "
                "No lessons were modified.",
                err=True,
            )
            raise typer.Exit(code=2)

        _review_staged_lessons(brain_root)

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
    utilization: bool = typer.Option(
        False, "--utilization",
        help="Join schema-1.2 AutoRecall hits to their Claude Code transcripts: "
             "were the injected docs actually opened? Replaces the base report "
             "(rather than appending to it) and writes an LLM-judge sample.",
    ),
    sample_out: Optional[Path] = typer.Option(
        None, "--sample-out",
        help="Where to write the --utilization LLM-judge sample JSON "
             "(default: <XDG cache>/recall/utilization_sample.json; never inside the "
             "brain, because it holds raw prompt and response text).",
    ),
    sample_n: int = typer.Option(
        24, "--sample-n",
        help="Max cases to write to the --utilization LLM-judge sample.",
    ),
    brain_root: Optional[Path] = typer.Option(
        None, "--brain-root",
        help="Brain root for --utilization path normalization "
             "(default: $BRAIN_ROOT or ~/.agent).",
    ),
):
    """Auto-recall coverage, latency and utilization (+ cross-source tool-call visibility).

    Pass `--utilization` for the per-prompt injected-document breakdown.

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

    if utilization:
        from recall.utilization import compute_utilization, render_utilization

        try:
            util_since_ts_ms = parse_since(since or "14d")
        except ValueError as e:
            typer.echo(f"recall stats: {e}", err=True)
            raise typer.Exit(code=2)
        td = transcripts_dir or (Path.home() / ".claude" / "projects")
        # `--brain-root` wins; otherwise defer to the one resolver
        # ($BRAIN_ROOT, else the brain home, else its parent when that
        # home is the `memory/` dir). Re-deriving it here would report
        # utilization against ~/.agent for anyone whose brain lives
        # elsewhere.
        from recall.config import brain_root as _config_brain_root
        effective_brain_root = brain_root or _config_brain_root()
        # Raw prompt/response text must never land under the brain root: sync.sh
        # would push it to the remote on the next hourly tick (runtime/core/events.py
        # data policy). Default to the recall cache dir instead.
        from recall.config import cache_dir as _cache_dir
        effective_sample_out = sample_out or (_cache_dir() / "utilization_sample.json")
        util_report = compute_utilization(
            log_path, td,
            brain_root=effective_brain_root,
            since_ts_ms=util_since_ts_ms,
            sample_n=sample_n,
            sample_out=effective_sample_out,
        )
        if json_out:
            from dataclasses import asdict
            data = asdict(util_report)
            data["used_docs_top"] = [list(t) for t in data["used_docs_top"]]
            typer.echo(_json.dumps(data, indent=2))
        else:
            typer.echo(render_utilization(util_report))
        return

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


@app.command()
def trace(
    target: str = typer.Argument(
        ..., help="Lesson to trace: a slug, a substring of the name, or a file path."
    ),
    brain_root: Optional[Path] = typer.Option(
        None,
        "--brain-root",
        help="brainstack memory root. Default: ~/.agent",
    ),
) -> None:
    """Walk a recalled lesson's provenance chain back to its source.

    Answers "where did this lesson come from, and can I trust it?" by reading
    the lesson's frontmatter and printing: its provenance label, who/what
    wrote it (source / created_by), the originating session, whether a human
    reviewed it (reviewed_by) or it is still staged (needs_review), and any
    evidence ids / source candidate recorded at graduation. When the lesson
    names a session that an indexed digest also references, that digest is
    surfaced as a pointer.

    A lesson with no provenance fields (a pre-0.6 file) is reported honestly
    as "provenance: none" rather than fabricating a trail.
    """
    from recall.frontmatter import parse_path

    root = brain_root.expanduser() if brain_root else resolve_brain_home()
    # resolve_brain_home() may return the brain root or its memory/ subdir
    # depending on config; normalize to the dir that contains memory/.
    if (root / "memory").is_dir():
        brain = root
    elif root.name == "memory":
        brain = root.parent
    else:
        brain = root
    lessons_dir = brain / "memory" / "semantic" / "lessons"

    # Resolve the target to a lesson file: explicit path first, then exact
    # slug, then case-insensitive substring of the stem (same resolution
    # spirit as `recall forget`).
    path: Optional[Path] = None
    cand = Path(target).expanduser()
    if cand.is_file():
        path = cand
    elif (lessons_dir / f"{target}.md").is_file():
        path = lessons_dir / f"{target}.md"
    elif lessons_dir.is_dir():
        matches = sorted(
            p for p in lessons_dir.glob("*.md")
            if target.lower() in p.stem.lower()
        )
        if len(matches) == 1:
            path = matches[0]
        elif len(matches) > 1:
            typer.echo(f"'{target}' matches {len(matches)} lessons:", err=True)
            for p in matches:
                typer.echo(f"  {p.stem}", err=True)
            typer.echo("Narrow the slug or pass a file path.", err=True)
            raise typer.Exit(2)

    if path is None or not path.is_file():
        typer.echo(
            f"trace: no lesson matches '{target}' under "
            f"{lessons_dir} (and it is not a file path).",
            err=True,
        )
        raise typer.Exit(2)

    fm = parse_path(path).frontmatter or {}

    source = fm.get("source")
    created_by = fm.get("created_by")
    provenance = fm.get("provenance")
    created = fm.get("created")
    reviewed_by = fm.get("reviewed_by")
    needs_review = fm.get("needs_review")
    review_reason = fm.get("review_reason")
    # session id can appear under the remember/digest field or the migration
    # extension field.
    session_id = fm.get("session_id") or fm.get("source_session_id")
    evidence_ids = fm.get("evidence_ids") or []
    source_candidate = fm.get("source_candidate")
    reviewer = fm.get("reviewer")  # graduate.py rows

    has_trail = any(
        v not in (None, [], "")
        for v in (source, created_by, provenance, reviewed_by, needs_review,
                  session_id, evidence_ids, source_candidate, reviewer)
    )

    typer.echo(f"== trace: {path.stem} ==")
    typer.echo(f"  file: {path}")
    if not has_trail:
        typer.echo("  provenance: none (no provenance fields recorded on this lesson)")
        return

    typer.echo(f"  provenance: {provenance or 'none'}")
    if source:
        typer.echo(f"  source: {source}")
    if created_by:
        typer.echo(f"  created_by: {created_by}")
    if created:
        typer.echo(f"  created: {created}")

    # Review status: durable (human-reviewed) vs staged (awaiting review).
    if reviewed_by:
        typer.echo(f"  reviewed_by: {reviewed_by} (durable)")
    elif reviewer:
        typer.echo(f"  reviewer: {reviewer} (graduated)")
    elif needs_review:
        reason = f" ({review_reason})" if review_reason else ""
        typer.echo(f"  needs_review: true{reason} (staged, not yet human-reviewed)")

    if session_id:
        typer.echo(f"  session_id: {session_id}")
        digests_dir = brain / "memory" / "semantic" / "digests"
        if digests_dir.is_dir():
            hits = sorted(
                p for p in digests_dir.glob("*.md")
                if session_id in p.name or session_id in _safe_head(p)
            )
            for p in hits[:3]:
                typer.echo(f"    originating digest: {p}")
            if not hits:
                typer.echo("    originating digest: none found in this brain")

    if source_candidate:
        typer.echo(f"  source_candidate: {source_candidate}")
    if evidence_ids:
        shown = ", ".join(str(e) for e in list(evidence_ids)[:5])
        more = "" if len(evidence_ids) <= 5 else f" (+{len(evidence_ids) - 5} more)"
        typer.echo(f"  evidence_ids: {shown}{more}")

    typer.echo(
        "  note: provenance is self-reported by the writer, not signed. "
        "It records where a lesson claims to come from, not that the claim is true."
    )


def _safe_head(path: "Path", n: int = 4096) -> str:
    """Read the leading bytes of a file for a substring scan; '' on error."""
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read(n)
    except (OSError, ValueError):
        return ""


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
