# recall

**Read-side retriever for brainstack. CLI + MCP server.**

`recall` retrieves relevant memories from a brain directory using hybrid search
(BM25 + embeddings, fused with Reciprocal Rank Fusion). It's the read-side
companion to brainstack's write-side (`agent/dream`, `agent/memory`, etc).

The same retriever also works standalone against any directory of markdown
notes — Obsidian vaults, Logseq graphs, plain notes — so the brain you use
with brainstack and the brain you keep in your second-brain repo can share
one search interface.

## Zero-config inside brainstack

If you installed brainstack, `recall` reads `$BRAIN_ROOT` (the same env var
brainstack's `install.sh` sets) and indexes `$BRAIN_ROOT/memory/`
automatically. **You do not need to set up anything separately.**

```bash
# Inside the brainstack repo's venv
pip install -e '.[embeddings,mcp]'
recall reindex
recall query "how do I avoid context bloat from reading too many files"
```

The auto-generated config at `~/.config/recall/config.json` expands
`$BRAIN_ROOT/memory` correctly and excludes `episodic/`, `working/`, and
`candidates/` (the write-side staging dirs that aren't graduated lessons yet).

To override the brain location explicitly (e.g., point at an Obsidian vault),
set `BRAIN_HOME` — it takes precedence over `BRAIN_ROOT`.

## Retrieval quality

Numbers from the synthetic-corpus benchmark in `tests/recall/bench_e2e.py`
(deterministic seed, scales 80 / 1k / 5k). Hybrid retrieval = Qdrant Prefetch
over `BAAI/bge-base-en-v1.5` (dense, 768d) + `Qdrant/bm25` (sparse, IDF), fused
with `Fusion.RRF`.

See `tests/recall/BENCH_RESULTS.md` for the full per-strategy table. Headline:

| Brain size | Bucket-recall@5 paraphrase, no recall | with Qdrant hybrid |
|---|---|---|
| 80 lessons | ~56% | ~100% |
| 1,000 lessons | ~38% (full MEMORY.md), ~12% (200-line truncation) | ~100% |
| 5,000 lessons | numbers in BENCH_RESULTS.md | numbers in BENCH_RESULTS.md |

"Lexical" = the query shares key tokens with the target's frontmatter
`description`. "Paraphrase" = it doesn't (e.g., "what runs when I
`kubectl apply`" vs. a memory titled `kubernetes-pods`). Use `recall eval`
against your own private cases before changing retrieval defaults; rerankers
can help targeted semantic queries while hurting broad workflow prompts.

## CLI

```
recall query "..."                  Top-K results as JSON
recall query --strategy context "..."  Balanced context for broad prompts
recall query --expand "..."         LLM-expand the query for hard semantic prompts
recall query --source brain "..."   Filter to a specific source
recall query --type feedback "..."  Filter by frontmatter type
recall query --k 10 "..."           Custom result count
recall reindex                      Rebuild ranking caches
recall eval cases.jsonl --strategy context --k 5  Measure a retrieval strategy
recall sources                      List configured sources
recall doctor                       Diagnose missing deps / broken paths
```

`query`, `reindex`, `eval`, `sources`, and `doctor` (without subflags) are
read-only. They never modify configured brain files; `query`, `reindex`, and
`eval` may update the local retrieval cache under `$XDG_CACHE_HOME/recall`.

`recall query` defaults to `--strategy ranked`: one ranked list for exact
lookup. Use `--strategy context` for broad agent prompts; it decomposes the
prompt into focused subqueries, gives each intent a coverage slot, dedupes, and
then fills the remaining slots. Auto-recall and MCP use context strategy by
default because they feed LLM context, not a human search results page.

### `--expand` for hard semantic queries

When the user phrases a question with vocabulary the target doc doesn't use
(e.g. "look over what I've been touching" → doc titled "reviewed-X-changes"),
hybrid retrieval alone misses. `--expand` asks the configured LLM provider
(via the brainstack provider registry; see `agent/tools/llm_providers/`) for N
paraphrases, retrieves each one separately, fuses the ranked lists with
Reciprocal Rank Fusion (k=60), and (if `--rerank cross_encoder` is on) reranks
the union against the original query.

Empirical lift on a 38-query LLM-paraphrased hard set against a 744-doc real
brain:

| Config | Recall@1 | Recall@5 | Recall@10 | NDCG@10 | p50 |
|---|---|---|---|---|---|
| Baseline (no expand, no rerank) | 13.2% | 36.8% | 44.7% | 26.0% | 24 ms |
| `--expand` only (RRF fusion) | 5.3% | 52.6% | **68.4%** | 36.8% | 130 ms |
| `--expand` + `--rerank cross_encoder` | 15.8% | 50.0% | 68.4% | 39.2% | 951 ms |
| `--expand` + bge-reranker-base post-rerank | 26.3% | 47.4% | 63.2% | 42.1% | 2.5 s |
| `--expand` + jina-v2-base post-rerank | 26.3% | 63.2% | 78.9% | **50.1%** | 30 s |

The expansion is cached per (query, n, provider) within the process, so
sessions that repeat a query don't pay the LLM round-trip twice. On any
provider failure (timeout, missing CLI, network) `--expand` falls back to the
original query alone — no hard error.

Use `--expand` when:

- The query is semantic / question-shaped, not a keyword lookup
- The brain has hand-written content where authors and querier might use
  different vocabulary (digests, lessons, plans)
- You can pay one LLM call worth of latency per query

Skip `--expand` when:

- The query already shares vocabulary with the target (project names, exact
  symbol names, file paths)
- You need sub-200ms p50 (the LLM call adds latency)
- You're in a no-network / no-LLM-CLI environment

`recall eval` reads JSONL cases:

```json
{"query":"crash safe file writes","expected":{"name":"atomic-writes"},"kind":"lexical"}
{"query":"avoid surprise package upgrades","expected":{"path":"semantic/lessons/pin-dependencies.md"}}
{"query":"incident response runbook","expected":{"any_of":["rollback-runbook","incident-runbook"]}}
{"query":"setup safety and branch protection","expected":{"all_of":[{"any_of":["setup-backup","migration-backup"]},{"name":"branch-protection"}]},"kind":"broad"}
```

It prints a JSON summary with `recall_at_1`, `recall_at_k`, `mrr`, group
coverage for broad prompts, misses, and latency. Use `--details results.jsonl`
for per-case results.

Embedded Qdrant uses a local filesystem lock. Short-lived CLI calls wait up to
2 seconds by default; override with `RECALL_QDRANT_LOCK_TIMEOUT=<seconds>` if
your machine needs a different tradeoff.

Long-lived entrypoints such as `recall-mcp` keep one embedded client open for
warm queries. Separate recall processes still contend on the embedded Qdrant
filesystem lock; heavy concurrent retrieval should use a shared recall/Qdrant
service or separate `XDG_CACHE_HOME` values.

## MCP server

Register `recall-mcp` with your MCP-aware client. Single tool exposed:
`recall_query`. Identical JSON contract to the CLI.

```json
{
  "mcpServers": {
    "recall": {
      "command": "recall-mcp"
    }
  }
}
```

Works with Claude Code, Cursor, Cline, Continue, Zed, and any other MCP-aware
client.

## Configuration

Auto-generated at `$XDG_CONFIG_HOME/recall/config.json` (defaults to
`~/.config/recall/config.json`) on first `recall query`. You don't need to
open it for the brainstack-installed case. Example for advanced overrides:

```jsonc
{
  "sources": [
    {
      "name": "brain",
      // Plain `$BRAIN_ROOT` / `$BRAIN_HOME` are expanded; `${VAR:-default}` shell syntax is NOT.
      "path": "$BRAIN_ROOT/memory",
      "glob": "**/*.md",
      "frontmatter": "auto-memory",
      "exclude": ["episodic/**", "candidates/**", "working/**", "scripts/**"]
    }
  ],
  "ranking": {
    "mode": "hybrid",                                    // "hybrid" | "dense" | "sparse"
    "embedder": "BAAI/bge-base-en-v1.5",                   // dense embedder (FastEmbed model name)
    "sparse_embedder": "Qdrant/bm25",                      // sparse / BM25-IDF encoder
    "reranker": "none",                                    // "none" | "cross_encoder" — opt-in
    "reranker_model": "jinaai/jina-reranker-v1-turbo-en",  // used when reranker="cross_encoder"
    "rerank_n": 20                                         // candidates fed to the reranker
  },
  "default_k": 5
}
```

### Adding a second-brain repo (Obsidian, Logseq, plain notes)

Append a source. Frontmatter is optional for these:

```json
{
  "name": "obsidian",
  "path": "~/Documents/Obsidian/Vault",
  "glob": "**/*.md",
  "frontmatter": "optional",
  "exclude": [".obsidian/**", "templates/**"]
}
```

## Safety

- **Retrieval is read-only.** `recall` never modifies files in the brain.
- **Caches live elsewhere.** Indexes go in `$XDG_CACHE_HOME/recall/`, never
  inside the brain.
- **Symlink containment.** `.md` files that resolve outside the source root
  are skipped.
- **Adversarial-input hardened.** YAML frontmatter is capped at 256 KiB and
  parsed with `safe_load` (defeats billion-laughs and `!!python/object`
  exploits).

## Architecture

```
recall/
├── frontmatter.py     # YAML parsing — adversarial-input hardened
├── config.py          # XDG paths, BRAIN_ROOT/BRAIN_HOME resolution, RankingConfig
├── sources.py         # file discovery, glob, exclude, symlink containment
├── qdrant_backend.py  # Qdrant embedded client + FastEmbed dense/sparse models
├── core.py            # HybridRetriever facade + context strategy
├── index.py           # build_index / load_index / needs_refresh (delegates)
├── migrate.py         # two-layer-backup migration plan + verify + rollback
├── serialize.py       # JSON-safe coercion (YAML dates → ISO strings)
├── cli.py             # Typer CLI — query / reindex / sources / doctor
└── mcp_server.py      # MCP wrapper: exposes recall_query as one tool
```

Retrieval pipeline at query time:

1. Embed the query with `BAAI/bge-base-en-v1.5` (dense, 768-dim) and `Qdrant/bm25`
   (sparse, IDF-weighted) via FastEmbed.
2. Qdrant `Prefetch` runs both legs against the per-source collection (top-20 each).
3. `Fusion.RRF` fuses the two ranked lists. Type/source filters are applied as
   Qdrant payload conditions inside the prefetch.
4. **Context strategy** (LLM-facing): for broad prompts, decompose into focused
   subqueries, retrieve each one, reserve coverage slots, dedupe, and then fill
   by fused rank. CLI exact lookup stays on ranked strategy unless you pass
   `--strategy context`.
5. **Cross-encoder rerank** (opt-in): pass `--rerank cross_encoder` or set
   `"reranker": "cross_encoder"` in config.json to enable a third stage that
   scores `(query, doc)` pairs together via `jinaai/jina-reranker-v1-turbo-en`.
   Adds hundreds of milliseconds to about a second per query, depending on
   cache warmth and candidate count. Real-brain testing showed mixed results:
   helps when the query has clear semantic intent but can push correct answers
   out of top-3 when the bi-encoder already had a good ranking. Default off.
6. Final top-K returned as JSON. Results are deserialized from each point's
   payload back into `Document` + `QueryResult` shapes.

Storage at `$XDG_CACHE_HOME/recall/qdrant/` (embedded mode, no daemon, no Docker).

## Standalone usage (without brainstack)

`recall` works against any directory of markdown notes:

```bash
export BRAIN_HOME="$HOME/.local/share/brain"
mkdir -p "$BRAIN_HOME/notes"
recall reindex
recall query "..."
```

Frontmatter is optional in standalone mode — `recall` falls back to filename
+ first-H1 + body. The `auto-memory` frontmatter convention (`name`,
`description`, `type`) gets a 3× description weight in the index, which is
why brainstack-formatted brains rank slightly better than plain markdown.

## License

This package is part of brainstack and falls under the project's top-level
LICENSE (Apache-2.0). See `../LICENSE` and `../NOTICE`.
