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

Numbers from the parametrized test suite (40-doc synthetic corpus, 120+ queries):

| Mode | Recall@5 (lexical) | Recall@5 (paraphrase) | Recall@5 (overall) |
|---|---|---|---|
| BM25-only | ≥ 90% | ~40% | ≥ 60% |
| Hybrid (BM25 + embeddings) | ~100% | ~70% | ≥ 85% |

"Lexical" = the query shares key tokens with the target's frontmatter
`description`. "Paraphrase" = it doesn't (e.g., "what runs when I
`kubectl apply`" vs. a memory titled `kubernetes-pods`). For a 90 MB
general-purpose embedding model, ~70% on hard paraphrases is consistent
with what other small-model RAG systems achieve. The optional
`[embeddings]` extra is recommended.

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
    "bm25_weight": 1.0,        // currently a toggle: any value > 0 enables BM25
    "embedding_weight": 1.0,   // any value > 0 enables embeddings if installed
    "embedding_model": "all-MiniLM-L6-v2"
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
  exploits). Cache manifests are defensively typed.
- **Concurrent-reindex safe.** Index writes use per-process tmp filenames +
  atomic rename.

## Architecture

```
recall/
├── frontmatter.py   # YAML parsing — adversarial-input hardened
├── config.py        # XDG paths, BRAIN_ROOT/BRAIN_HOME resolution
├── sources.py       # file discovery, glob, exclude, symlink containment
├── core.py          # BM25Plus + sentence-transformers + Reciprocal Rank Fusion
├── index.py         # cache: build, load, refresh-on-stale, atomic writes
├── migrate.py       # two-layer-backup migration plan + verify + rollback
├── serialize.py     # JSON-safe coercion (YAML dates → ISO strings)
├── cli.py           # Typer CLI — query / reindex / sources / doctor
└── mcp_server.py    # MCP wrapper: exposes recall_query as one tool
```

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
