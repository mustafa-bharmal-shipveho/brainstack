# brainstack

**A model-independent, git-synced brain for AI coding agents. Keep the same
memory when you switch between Claude Code, Codex CLI, Cursor, or any
MCP-capable tool.**

Brainstack is not another assistant and it is not tied to one model vendor. It
is a local memory substrate: plain files under `~/.agent/`, portable recall via
CLI/MCP, human-reviewed durable lessons, and optional runtime telemetry that can
replay what entered an agent's context.

The core question brainstack helps answer:

> **Did the agent have the right memory/context when it mattered?**

## Why Brainstack

- **Model-independent memory.** Claude today, Codex tomorrow, Cursor or another
  MCP-capable client later. The memory stays yours.
- **Local, inspectable source of truth.** Memories are markdown and JSONL files
  in `~/.agent/`, not an opaque vendor-owned store.
- **Human-gated long-term lessons.** Agents can stage candidates, but durable
  memory promotion is reviewed by you.
- **Safe migration path.** Existing Claude Code, Cursor, and Codex memories can
  be imported without deleting the source data; Claude memory swaps preserve a
  timestamped backup.
- **Retrieval that scales past one giant memory file.** `recall` uses hybrid
  Qdrant + BM25 search over lessons, digests, notes, and imported markdown.
- **Measurable behavior.** `recall stats` reports fire/skip counts, latency,
  surfaced source mix, and optional Claude Code tool-call breakdowns.

If your team only uses one AI client and is happy with that client's native
memory, brainstack may be more machinery than you need. Use it when memory
should outlive a specific model, tool, laptop, or vendor account.

## Quickstart

**Prereqs:** `git`, Python 3.10+, macOS or Linux, and a private git remote you
control for your brain mirror. Claude Code is optional unless you want the
Claude runtime hooks.

```bash
git clone https://github.com/<your-org>/brainstack.git
cd brainstack

./install.sh --brain-remote git@github.com:<you>/<your-private-brain-repo>.git \
             --push-initial-commit
```

Omit `--push-initial-commit` if your private brain remote already has history.

After the installer adds `recall` to your PATH:

```bash
recall remember "always run the exact CI command from the repo config"
recall query "what should I remember before changing CI?"
recall forget ci-command
```

Optional Claude Code runtime hooks:

```bash
recall runtime install-hooks
```

`install.sh` itself does not edit `~/.claude/`; runtime hook installation is a
separate explicit step. Setup details: [`docs/claude-code-setup.md`](docs/claude-code-setup.md).

## Bring Existing Memories

A fresh install creates `~/.agent/`. Existing Claude Code, Cursor, and Codex CLI
memories are not silently imported.

Recommended ongoing import:

```bash
./install.sh --setup-auto-migrate
```

One-time snapshot import:

```bash
./install.sh --migrate
./install.sh --migrate ~/.claude/projects/<slug>/memory
```

For Claude Code memory directories, migration preserves the original at
`<source>.bak.<timestamp>` before wiring the source into brainstack. Cursor and
Codex imports are snapshot-only unless you enable `--setup-auto-migrate`.

Optional deeper Claude Code mirroring:

```bash
./install.sh --setup-claude-extras
```

That mirrors Claude transcripts and misc directories into `~/.agent/imports/`
without modifying the source directories.

## What It Does

Brainstack has four main layers:

| Layer | Purpose |
|---|---|
| **Storage** | Captures agent events into `~/.agent/memory/episodic/`; nightly dream cycles cluster them into reviewable candidates. |
| **Digests** | Summarizes long sessions into searchable markdown so recall finds past work, not only raw tool calls. |
| **Retrieval** | `recall query` and `recall-mcp` search memory/imports with hybrid Qdrant + BM25 retrieval. |
| **Runtime** | Optional context runtime for budgets, eviction policy, and replay of what brainstack injected. |

Useful commands:

| Command | Use |
|---|---|
| `recall query "..."` | Search memory and imports. |
| `recall remember "..."` | Write a durable lesson. |
| `recall forget <query>` | Archive a lesson by name/substr match. |
| `recall pending --review` | Human review for staged memory candidates. |
| `recall reindex` | Rebuild retrieval cache after large imports/edits. |
| `recall stats --since 7d` | Inspect auto-recall usage and latency. |
| `recall runtime replay` | Reconstruct runtime context state from logs. |

Retrieval details and benchmark notes: [`recall/README.md`](recall/README.md).
Runtime design: [`docs/runtime.md`](docs/runtime.md).

Optional features:

```bash
./install.sh --setup-digests       # summarize sessions into searchable digests
./install.sh --enable-auto-recall  # Claude Code: retrieve memories per prompt
recall-mcp                         # expose recall to MCP-capable clients
```

## Review Flow

The dream cycle stages candidate lessons in `~/.agent/memory/candidates/`.
Nothing becomes durable semantic memory until you review it.

```bash
recall pending
recall pending --review
```

Optional startup surfaces:

```bash
./install.sh --setup-pending-review-all
./install.sh --remove-pending-review-all
```

These wire pending-review visibility into Claude Code, Cursor rules, and shell
wrappers for AI CLIs listed in `~/.agent/banner/wrapped_tools`.

## Tell Your Agents to Use Recall First

Recall only helps if the agent calls it. Without explicit instructions, hosts
(Claude Code, Codex CLI, Cursor) tend to default to grep / Minerva / web
search — even when the answer is already in your brain.

```bash
./install.sh --setup-recall-first-all      # all three host configs
./install.sh --remove-recall-first-all
```

This injects a brainstack-managed block into:

- `~/.claude/CLAUDE.md` — Claude Code
- `~/.codex/AGENTS.md`  — Codex CLI
- `~/.cursor/.cursorrules` — Cursor

with a directive telling the agent: for prior-personal-context questions
("have I dealt with X before?", "what did I learn about Y?"), call `recall
query "..."` (or the `recall_query` MCP tool) FIRST. The block is delimited
by `<!-- brainstack-recall-first-start -->` sentinels and is idempotent — re-
running replaces the bracketed section without touching anything else.

Per-host variants if you only want one: `--setup-recall-first-claude`,
`--setup-recall-first-codex`, `--setup-recall-first-cursor` (each with a
matching `--remove-*`).

## Add More Sources

Mirror any folder into recall:

```bash
./install.sh --add-source ~/Documents/Engineering-Notes --as kb/eng-notes
./install.sh --list-sources
./install.sh --remove-source kb/eng-notes
```

Mirrored sources land under `~/.agent/imports/` and are included in recall by
default. They are also synced to your private brain remote unless you add the
destination to `~/.agent/.gitignore`.

## Safety Model

Brainstack assumes your memory may contain sensitive tool-call history.

- Installs a redaction pre-commit hook.
- Runs sync-time JSONL scrubbing before push.
- Refuses scheduled sync without `trufflehog` or `gitleaks`.
- Supports `redact-private.txt` for your own patterns.
- Uses sentinel locks and atomic writes for append/rewrite paths.
- Keeps migration backups instead of deleting existing memory directories.

Threat model and redaction policy: [`docs/redaction-policy.md`](docs/redaction-policy.md).
Git sync details: [`docs/git-sync.md`](docs/git-sync.md).

## Known Boundaries

- Embedded Qdrant is a local cache, not a multi-writer database. Short CLI/MCP
  use is locked and safe; heavy concurrent agents should use separate
  `XDG_CACHE_HOME` values or a shared recall/Qdrant service.
- Broad natural-language queries may return the right neighborhood rather than
  the exact memory. Treat lower-score results as context, not authority.
- Auto-recall is currently Claude-Code-specific because it depends on Claude
  Code's `UserPromptSubmit` hook surface. Other clients can still call recall
  via CLI or MCP; per-prompt auto-injection for other clients needs an adapter
  for that client's hook/rules surface.
- The runtime audits what brainstack injects. It cannot inspect or control a
  model vendor's private KV cache.

## Architecture

```
~/.agent/
├── memory/
│   ├── episodic/      # append-only captured events
│   ├── candidates/    # staged lessons awaiting review
│   ├── semantic/      # durable lessons, digests, claims
│   ├── personal/      # profile, notes, references
│   └── MEMORY.md      # generated index
├── imports/           # mirrored external sources
├── tools/             # dream, sync, migration, redaction helpers
├── runtime/           # optional context-runtime logs, populated after runtime use
└── .git/              # private brain repo
```

More detail:

- [`docs/architecture.md`](docs/architecture.md)
- [`docs/memory-model.md`](docs/memory-model.md)
- [`docs/dream-cycle.md`](docs/dream-cycle.md)
- [`docs/operational-notes.md`](docs/operational-notes.md)
- [`docs/runtime.md`](docs/runtime.md)
- [`recall/README.md`](recall/README.md)

## Upgrade

After pulling a newer brainstack checkout, refresh installed framework code
without touching user memories:

```bash
./install.sh --upgrade
```

This updates `~/.agent/tools`, framework memory modules, and runtime helpers
while preserving user data under `~/.agent/memory/`.

## License

Apache 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

This is infrastructure shared as-is. Issues and PRs are welcome, but no support
obligations are implied.

## Provenance

Brainstack vendors files derived from
[codejunkie99/agentic-stack](https://github.com/codejunkie99/agentic-stack),
primarily in the dream cycle, clustering, and lesson-rendering pipeline. See
[`NOTICE`](NOTICE) and [`UPSTREAM.md`](UPSTREAM.md) for attribution, file list,
and modification notes.
