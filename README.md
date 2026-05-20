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

Prereqs: `git`, Python 3.10+, macOS/Linux, a private git remote for your brain.

```bash
git clone https://github.com/<your-org>/brainstack.git
cd brainstack
./install.sh --brain-remote git@github.com:<you>/<your-private-brain-repo>.git \
             --push-initial-commit
```

That's it. The installer will:

- Discover existing Claude Code / Codex CLI / Cursor memory and ask before importing
- Schedule hourly sync + nightly dream consolidation (raw events → digests)
- Wire the recall-first directive into Claude Code, Codex CLI, and Cursor
- Enable auto-recall on Claude Code (every prompt auto-surfaces brain context)

Open Claude Code / Codex CLI / Cursor and ask a question — your brain context
surfaces in their replies automatically.

Verify: `recall doctor`. Skip any default: see [Customize your install](#customize-your-install). Remove everything: `./uninstall.sh`.

## Customize your install

The defaults install everything. Skip any subset by passing `--no-X` to `./install.sh`:

| Flag | Skips | Reason you might |
|---|---|---|
| `--skip-migrate` | Interactive scan-and-import of existing Claude / Codex / Cursor memory | Start with an empty brain |
| `--no-auto-migrate` | Background scanner that pulls new agent sessions into the brain | Trigger migrate manually instead |
| `--no-launchd` | Hourly sync + nightly dream LaunchAgents | You're on Linux, or want to script the schedule |
| `--no-recall-first` | Recall-first directive in `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, `~/.cursor/.cursorrules` | You don't use those agents, or wire elsewhere |
| `--no-auto-recall` | Claude Code UserPromptSubmit hook firing recall on every prompt | Want only agent-driven recall, not the unconditional sweep |
| `--yes` | (Accept all migrate-discovery prompts non-interactively) | CI / scripted installs |
| `--no-prompt` | (Decline all migrate prompts; still runs the other 4 defaults) | CI / scripted installs |

Each opt-out is reversible later via the matching `--setup-X` / `--enable-X` flag (or `--remove-X` / `--disable-X` to undo something later). Run `recall doctor` any time to see what's enabled.

## Bring Existing Memories

The default install discovers `~/.claude/projects/*/memory`, `~/.codex/`, and
`~/.cursor/` and prompts y/n before importing each. If you skipped that
(via `--skip-migrate` / `--no-prompt`) or want to import additional sources
manually later:

```bash
./install.sh --migrate                                    # interactive discovery
./install.sh --migrate ~/.claude/projects/<slug>/memory   # specific path
```

For Claude Code memory directories, migration preserves the original at
`<source>.bak.<timestamp>` before wiring the source into brainstack as a
symlink. Cursor and Codex imports are snapshot-style.

The default install also turns on a background scanner (`--setup-auto-migrate`)
that continuously rolls newly-written agent sessions into the brain. If you
opted out via `--no-auto-migrate`, enable it later with
`./install.sh --setup-auto-migrate`.

Optional deeper Claude Code mirroring (transcripts + misc dirs into
`~/.agent/imports/`, no modifications to source):

```bash
./install.sh --setup-claude-extras
```

## Setup details

Most users won't need this section — the default install handles it. Read on
if you want to understand what's running, opt out of pieces, or troubleshoot.

### Requirements detail

- `git`, Python 3.10+, macOS or Linux
- A private git remote you control (your brain's mirror)
- **~2 GB free disk** for the first `recall reindex` (one-time download of
  the ~440 MB BGE-base embedding model under `~/.cache/fastembed/`, plus
  Qdrant cache)
- **Network** for the first model download — subsequent queries are offline
- **Recommended**: `trufflehog` or `gitleaks` on PATH (required for hourly
  git sync). Pass `--install-scanner` during setup, or `brew install trufflehog`.
- **Optional**: `claude` or `codex` CLI for `recall query --expand` (default
  on; LLM round-trip improves hard semantic queries. Without either, expand
  falls open silently and uses the original query.)

First-run note: the first `recall query` triggers a one-time reindex
(~30 s on a fast link). Subsequent queries are sub-3 s on a typical brain.
Per-feature retrieval details: [`recall/README.md`](recall/README.md).

### Hourly sync + nightly dream cycle (launchd)

The default install schedules these LaunchAgents. To wire them later (e.g.
after `--no-launchd`) or to tear down:

```bash
./install.sh --setup-launchd       # expands plist templates + launchctl load
./install.sh --remove-launchd      # unload + delete plists
```

The setup mode handles `REPLACE_HOME` / `REPLACE_PYTHON` substitution in the
plist templates (raw templates aren't usable as-is). Logs land at
`~/.agent/dream.log` and `~/.agent/sync.log`. Full sync architecture:
[`docs/git-sync.md`](docs/git-sync.md).

Linux: launchd isn't available; the default install silently skips this
mode and prints a note. Use a systemd timer if you want equivalent scheduling.

### Claude Code runtime hooks

```bash
recall runtime install-hooks
```

`install.sh` does not edit `~/.claude/settings.json` for safety; runtime hook
installation is a separate explicit step that you (or `--enable-auto-recall`)
opt into. Setup details: [`docs/claude-code-setup.md`](docs/claude-code-setup.md).

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
| `recall doctor` | Diagnose missing deps / broken paths / unreachable LLM providers — run this FIRST when something looks wrong. |
| `recall runtime replay` | Reconstruct runtime context state from logs. |

Retrieval details and benchmark notes: [`recall/README.md`](recall/README.md).
Runtime design: [`docs/runtime.md`](docs/runtime.md).

Additional features (default install already wires `--enable-auto-recall`):

```bash
./install.sh --setup-digests       # summarize sessions into searchable digests
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

The default install wires this for you (skip with `--no-recall-first`). What it
does and why: recall only helps if the agent calls it. Without explicit
instructions, hosts (Claude Code, Codex CLI, Cursor) tend to default to grep /
Minerva / web search — even when the answer is already in your brain.

To wire this later (e.g. after `--no-recall-first`) or to tear down:

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

## Manual brain edits via CLI

The daily way to use brainstack is through your host agent — Claude Code,
Codex CLI, or Cursor — which calls `recall` automatically. For times when
you want CLI access (adding a one-shot note from a terminal, debugging
retrieval, scripting against the brain), the `recall` command is your surface:

```bash
recall remember "always run the exact CI command from the repo config"
recall query "what should I remember before changing CI?"
recall forget ci-command
recall pending --review                  # review staged candidates
recall stats --since 24h                 # see auto-recall ROI
recall doctor                            # diagnose missing wiring
```

Full retrieval design and benchmarks: [`recall/README.md`](recall/README.md).

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

```bash
./upgrade.sh              # default: git pull + refresh brain (one command)
./upgrade.sh --no-pull    # skip the pull (you manage git yourself)
```

`./upgrade.sh` does two things:

1. `git pull --ff-only origin main` in the brainstack repo so you're on the
   latest released code. Use `--no-pull` if you're already on the version
   you want, or in a CI/release context that handles git separately.
2. Refreshes `~/.agent/tools`, framework memory modules, and runtime helpers
   while preserving user data under `~/.agent/memory/`. Equivalent to
   `./install.sh --upgrade`.

Each upgrade writes the new version to `~/.agent/.brainstack-version`, so
the next upgrade announces what changed (e.g., `Upgraded from 0.4.0 → 0.5.0`)
and points you at the [CHANGELOG](CHANGELOG.md) for details.

## Uninstall

Brainstack is safe to trial — uninstall removes every host-side surface it
installed and **preserves your memory data** by default. The brain at
`~/.agent/` (every digest, lesson, note you ever wrote) is the one thing the
uninstaller never touches without an explicit opt-in.

```bash
./uninstall.sh --dry-run    # print the plan, change nothing
./uninstall.sh              # interactive, with confirmation prompt
./uninstall.sh -y           # skip the prompt (non-interactive)
./uninstall.sh --purge-data # ALSO delete ~/.agent + ~/.config/{recall,brainstack}
```

(`./install.sh --uninstall ...` is the equivalent — `uninstall.sh` is a
discoverable wrapper around it.)

### What gets removed (default)

- `~/Library/LaunchAgents/com.user.agent-{dream,sync,migrate,claude-extras}.plist`
- The brainstack-shell-banner block in `~/.zshrc`
- brainstack-managed blocks in `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, `~/.cursor/.cursorrules`
- brainstack hook entries in `~/.claude/settings.json`
- The `recall` / `recall-mcp` symlinks under `~/.local/bin/`
- The model + index cache at `~/.cache/recall/` (regenerates on next reindex)

### What is preserved

- `~/.agent/` — your memory (digests, lessons, claims, notes, episodic logs)
- `~/.config/recall/` — your recall config
- `~/.config/brainstack/` — your extractor / project / channel configs
- Your remote brain repo on GitHub — the durable backup
- The brainstack repo clone (delete manually if you want to)

Run `./uninstall.sh --purge-data` (explicit opt-in) to also delete `~/.agent/`
and the config dirs.

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
