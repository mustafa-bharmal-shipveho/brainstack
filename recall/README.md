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
`kubectl apply`" vs. a memory titled `kubernetes-pods`). The remaining
paraphrase-recall gap on small corpora and outlier queries is expected to
close with the v0.3 cross-encoder reranker (see roadmap).

## CLI

```
recall query "..."                  Top-K results as JSON
recall query --source brain "..."   Filter to a specific source
recall query --type feedback "..."  Filter by frontmatter type
recall query --k 10 "..."           Custom result count
recall reindex                      Rebuild ranking caches
recall sources                      List configured sources
recall doctor                       Diagnose missing deps / broken paths
```

`query`, `reindex`, `sources`, and `doctor` (without subflags) are read-only.

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
    "reranker": "cross_encoder",                           // "cross_encoder" | "none"
    "reranker_model": "jinaai/jina-reranker-v1-turbo-en",  // FastEmbed cross-encoder
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
├── core.py            # HybridRetriever facade (delegates to qdrant_backend)
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
4. **Cross-encoder rerank** (default-on): the top-20 candidates are scored by
   `jinaai/jina-reranker-v1-turbo-en`, which scores `(query, doc)` pairs together
   instead of via independent embeddings — closes the semantic-distance gap that
   bi-encoders alone can't bridge. Latency cost: ~400 ms p50. Disable with
   `--rerank none` for sub-millisecond queries when paraphrase quality matters less.
5. Final top-K returned as JSON. Results are deserialized from each point's
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
