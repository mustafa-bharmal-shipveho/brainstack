# brainstack

**A persistent, git-synced brain for your AI coding agent — with a runtime that records, budgets, and replays what enters your agent's context each turn.**

Three layers, one stack:

- **Storage.** One global memory at `~/.agent/`. Every tool call → episodic log → nightly dream cycle clusters salient patterns → graduated lessons land in `semantic/` and are auto-loaded on every future session. Mistakes get codified once, never repeated.
- **Retrieval.** Hybrid recall (Qdrant + BM25) finds the right memory for any query. Tool-agnostic: works with Claude Code, Cursor, Codex CLI.
- **Runtime (v0.2).** Token budgets per bucket, eviction policy as a forkable Python file, full replay/audit. Answers *"why didn't the model know X?"* from artifacts instead of guesswork. **The runtime core is host-agnostic; v0.2 ships with the first adapter, Claude Code. Cursor and Codex CLI adapters slot into the same interface (community contributions welcome).** Today the runtime *records and replays* every injection decision a Claude Code session makes; it does not yet inject CLAUDE.md content itself (the v0.x roadmap wires that). What it controls is the log, the manifest, and the replay — which is enough to debug and prove behavior.

**Constant git sync.** Hourly push to your private remote (with required secret-scanner gate). Reinstall on a new machine and `git pull` brings back every lesson, every preference, every reference.

**One brain, every project.** Global `~/.agent/` (not per-repo), so a lesson learned debugging Postgres in repo A is available the next time you touch Postgres in repo Z.

The model gets smarter every release. Your agent only gets smarter if its context does. This is the substrate for that.

Built on top of [codejunkie99/agentic-stack](https://github.com/codejunkie99/agentic-stack) — vendored dream cycle, clustering, lesson rendering. See [`UPSTREAM.md`](UPSTREAM.md).

---

## Edit your second brain in one command

You know that thing where you have to keep telling Claude *"use /agent-team for development"* every. single. session? Or *"use `SELECT FOR UPDATE SKIP LOCKED` for queue claims, not table locks"* — and Claude forgets the moment the session ends?

Stop doing that. Tell brainstack once:

```bash
recall remember "always use /agent-team for development"
recall remember "use SELECT FOR UPDATE SKIP LOCKED for queue claims" --as postgres-locking
```

Both lessons land in `~/.agent/memory/semantic/lessons/` and brainstack auto-loads them on **every future session, forever**. That's the whole point of the second brain.

Changed your mind? Forget it back out by name (no cryptic IDs):

```bash
recall forget agent-team          # substring match — finds always-use-agent-team-for-development
recall forget postgres-locking    # exact slug works too
```

Forgotten lessons get moved to `~/.agent/memory/semantic/archived/<timestamp>-<name>.md` so you can `mv` them back if you want. Multi-match lists candidates rather than guessing.

**Four commands, two scopes:**

| Command | Lasts | Where |
|---|---|---|
| `recall remember "text"` | **forever** | `~/.agent/memory/semantic/lessons/` |
| `recall forget <query>` | **forever** | moves to `archived/` |
| `recall runtime add <file>` | one prompt (re-injection) | event log |
| `recall runtime evict <query>` | current session | event log |

The first two edit your **persistent brain** — what brainstack auto-loads forever. The last two control **the current session's context window** — for one-off "include this for this prompt" cases. Both use the same natural-query phrasing (basename, substring, or full path).

Run `recall remember --help` and `recall forget --help` for full options.

---

## The flight recorder for your AI session

Think of it like a flight recorder summary for one Claude Code session. After a long debugging session you can run `recall runtime timeline` and see *"Claude saw 108 files, but had to drop 76 of them because the memory bucket filled up 39 times."* That's how you know to either raise your budget, pin important items, or change which files you let Claude open.

```text
$ recall runtime timeline
Flight recorder for session "current" — 9 turns, 173 events.

Claude saw 112 files/tool results during this session.
78 were dropped because memory filled up (40 budget breaches).
34 items are still in memory.

Recent budget breaches:
  • turn 1 Read: dropped 5 items [c-02569426, c-08a64eec, c-0c8e88a1, c-16c0448f + 1 more]
  • turn 1 Read: dropped 1 item [c-3f1fcdc1]
  • turn 1 Bash: dropped 1 item [c-18c80ceb]
  • …and 35 more (run with --full to see every event)

Memory now:
  retrieved    33 items  19207 / 20000 tokens (96% full)
  scratchpad    1 item    7911 / 10000 tokens (79% full)
```

That answers "what happened in my session?" in 15 lines instead of 150.

For the deeper question — *which exact item did Claude forget at which exact turn?* — there's `--diff`:

## The bug nobody else can show you

You spend 40 minutes debugging a Postgres deadlock. At turn 6 you tell Claude the fix uses `SELECT FOR UPDATE SKIP LOCKED`. Forty turns later, after a context compaction, Claude proposes a fix that locks the whole table. You ask why. Claude doesn't know.

`recall runtime replay --diff 37:38`:

```text
turn 37 -> turn 38

evicted (1):
  - c-a3f0294b1c    (retrieved      280 tok) retrieved/turn-6-fix-summary.md
added (1):
  + c-77ab19d34e    (retrieved      412 tok) retrieved/postgres-locking-survey.md
unchanged: 11 items
```

There it is. The fix you taught Claude at turn 6 dropped out of the injection set on turn 38, evicted by the LRU policy after the compaction event rebuilt the warm tier. Two-line policy fix: pin items tagged `decision` for the session.

> *Synthetic example for illustration. With v0.2 installed, real `recall runtime replay --diff` output looks like this against actual session logs at `~/.agent/runtime/logs/`.*

No other tool can show you this. mem0 stores facts; we manage the working set. claude-obsidian writes a recap; we run the pager. Letta pages internally; we make every paging decision a JSON file you can read, diff, and version.

---

## Context runtime

The runtime layer (new in v0.2) owns the *injected* context — what is pushed into the agent's context window via hooks, CLAUDE.md, and re-injection. It does not own the model's KV cache or accumulated conversation history (those are opaque, no tool can manage them). What we own, we own deterministically and auditably.

### What you get

- **Manifest.** Every turn writes a JSON snapshot of what's in the injection set: bucket, source path, sha256, token count, retrieval reason, last-touched turn. Diffable. Versioned. Stable schema.
- **Token budgets.** Caps per bucket — `claude_md`, `hot`, `retrieved`, `scratchpad`. When a bucket exceeds its cap, the eviction policy fires.
- **Policy as code.** A single Python file at `runtime/core/policy/defaults/lru.py` that you read, fork, version. No YAML DSL. Defaults: LRU, recency-weighted, pinned-first.
- **Replay & audit.** Reconstruct turn-by-turn manifest evolution from any past session. Answer *"why didn't the model know X?"* from logs, not vibes.
- **Host-agnostic core; first adapter is Claude Code.** `runtime/core/` has zero Claude-specific imports — manifest, Engine, replay, policies all work for any host. The Claude Code wiring lives in exactly one place: `runtime/adapters/claude_code/`. Cursor and Codex CLI adapters drop in alongside via the same interface (open issue if you want to take one).
- **Reference-only by default.** Manifests log path + sha256 + token count, never raw content. Audit-by-default-leaks-secrets is the failure mode we engineered around. Raw capture is opt-in.

### Quickstart

```bash
# 1. Wire the Claude Code hooks (one-time, idempotent — preserves any
#    existing hooks you have)
recall runtime install-hooks

# 2. Run any Claude Code session normally — the runtime is silent.

# 3. Inspect what just happened
recall runtime tail              # last 10 events in plain English
recall runtime ls                # current manifest
recall runtime timeline          # flight-recorder summary
recall runtime timeline --full   # chronological detail
recall runtime replay --diff 5:6 # what entered / left between turns

# 4. Steer the current session: add a file or inline text into the next
#    prompt's re-injection block (no cryptic IDs — natural queries work)
recall runtime add ~/.agent/memory/semantic/lessons/postgres-locking.md
recall runtime add postgres-locking                  # resolves under brain root
recall runtime add "use /agent-team for this debugging session"  # inline text
recall runtime add policy --text                     # force text on a single word

# 5. Drop something out of the current manifest (same query phrasing)
recall runtime evict postgres-locking
recall runtime evict c-77ab                          # id prefix works
recall runtime evict postgres-locking --intent       # also skip on next re-injection

# 6. Pin items that should never be evicted
recall runtime pin c-a3f0294b1c
recall runtime budget                                 # caps + current usage
```

> **`recall runtime add` vs `recall remember`**: `runtime add` is *session-scoped* (one prompt, then gone). For permanent memory, use `recall remember` — see the section above.

### How re-injection works (opt-in)

Set `enable_reinjection = true` in `pyproject.toml` to activate the inject loop. On every `UserPromptSubmit`, the hook prepends a delimited block to your prompt summarising what you've added/pinned/evicted:

```text
<!-- runtime-reinject -->
User has marked these as always-relevant:
- postgres-locking.md (id=c-77ab19d3)
    use SELECT FOR UPDATE SKIP LOCKED for queue claims

User just added these for this turn:
- <inline-text> (id=c-b14215f4)
    use /agent-team for this debugging session
<!-- /runtime-reinject -->
```

Hard token cap (`reinjection_budget_tokens`, default 1500) prevents the block from blowing your context. The delimiters let you grep your prompts to see exactly what was added — no vendor magic. Default OFF; some Claude Code versions block hook stdout as prompt-injection protection, in which case the loop closes via `recall remember` writing to persistent memory instead.

### Configuration

```toml
[tool.recall.runtime]
log_dir = "~/.agent/runtime/logs"
capture_raw = false                       # default: reference-only, no leaks
enable_reinjection = false                # default OFF — opt-in to inject loop
reinjection_budget_tokens = 1500          # hard cap on the re-injection block

[tool.recall.runtime.budget]
claude_md = 4000
hot = 2000
retrieved = 20000
scratchpad = 10000
```

### Adapter status

brainstack is tool-agnostic at the storage and retrieval layers; the
runtime layer follows the same pattern (host-agnostic core + thin
per-host adapters), but only the Claude Code adapter ships in v0.2.

| Host | Storage | Retrieval | Runtime adapter |
|---|---|---|---|
| Claude Code | shipping | shipping | shipping (v0.2) |
| Cursor | shipping (`*.plan.md` ingest) | shipping | roadmap |
| Codex CLI | shipping (`rollout-*.jsonl` ingest) | shipping | roadmap |
| Aider, Cline, Windsurf, Continue | roadmap | roadmap | roadmap |

We started with Claude Code because it has the richest hook system
(`SessionStart`, `PostToolUse`, `Stop`, `PostCompact`, etc.) — the
easiest host to instrument first. Cursor and Codex CLI adapters need
their own design (different lifecycle models) but slot into the same
core via `runtime/adapters/<host>/`. Open an issue if you want to take
one.

### What it is not

- Not a vault. ([claude-obsidian](https://github.com/AgriciDaniel/claude-obsidian) does that.)
- Not a vector store. ([mem0](https://mem0.ai), [Zep](https://www.getzep.com), [Cognee](https://cognee.ai) do that.)
- Not an agent framework. ([Letta / MemGPT](https://www.letta.com) does that.)
- Not observability. ([Helicone](https://helicone.ai), [Langfuse](https://langfuse.com), [LangSmith](https://smith.langchain.com) do that.)
- Not a UI. CLI + JSON only.

It is the layer above storage and retrieval, below the agent loop, owning the budget and the eviction. That layer was empty.

### Honest boundary

We manage the *injection layer* — what we push into the context window via hooks, CLAUDE.md, and re-injection. We do not manage the model's KV cache or accumulated conversation history; those are opaque and cannot be evicted from mid-conversation. "Eviction" in our system means "demotion-from-injection on the next turn." That is the layer Claude Code actually lets you own, transparently and auditably. We do not claim more.

See [`docs/runtime.md`](docs/runtime.md) for the full design, schema reference, and roadmap.

---

## Quickstart

```bash
git clone https://github.com/mustafa-bharmal-shipveho/brainstack.git
cd brainstack

# One-shot install: creates ~/.agent/, wires it as a git repo with
# YOUR private remote, makes the initial commit, installs the
# pre-commit redaction hook.
./install.sh --brain-remote git@github.com:<you>/<your-private-brain-repo>.git \
             --push-initial-commit
```

`install.sh` also creates a venv at `.venv/`, runs `pip install -e .`, and symlinks the `recall` CLI into `~/.local/bin/`. After install, `recall runtime tail`, `recall runtime timeline`, etc. work as bare commands (assuming `~/.local/bin` is on your `$PATH` — the installer prints a one-line PATH-export tip if it isn't).

Then merge the printed snippet into `~/.claude/settings.json` (the installer never edits user config — see [`docs/claude-code-setup.md`](docs/claude-code-setup.md)).

Migrating from existing AI-tool memory dirs (Claude Code, Cursor, Codex CLI):

```bash
# Interactive — discovers what's on disk and lets you pick what to import:
./install.sh --migrate

# Or point at a specific source explicitly:
./install.sh --migrate ~/.claude/projects/<slug>/memory
```

For Claude Code's native auto-memory, `--migrate` defaults to swapping the source for a symlink to `~/.agent/memory` (with a timestamped backup) — so Claude Code's ongoing writes flow into the brain in real time. Cursor + Codex sources are ingested as snapshots (those tools keep writing to their own dirs).

### Set it once, forget it

```bash
./install.sh --setup-auto-migrate
```

Wizard asks which tools you use, installs ONE LaunchAgent that runs every hour and ingests new Cursor plans + Codex CLI sessions into the brain — sub-second incremental runs via offset-tracked idempotency. Claude Code is already automatic via the symlink. After this runs, you don't have to remember to migrate anything again.

Power users can drive it non-interactively: `--enable cursor-plans,codex-cli`, `--all`, `--none`, `--dry-run`, `--print-plist`. Tear-down: `./install.sh --remove-auto-migrate`.

Verify health any time with `./install.sh --verify` or `make report-status`.

---

## How it works

```
                                capture                distill                graduate              recall
   ┌─ Claude Code (real-time hook) ──────────► episodic/ ──────────► candidates/ ──────────► semantic/ ────► next session
   ├─ Cursor (hourly LaunchAgent) ─────────────►   JSONL log,         staged by              you review        auto-loaded
   └─ Codex CLI (hourly LaunchAgent) ──────────►   sentinel-locked    dream cycle            graduate.py /     via MEMORY.md
                                                                      (nightly)              reject.py         → CLAUDE.md

                                              ↻ git sync (hourly) — push to your private brain remote, scanner-gated
```

Five stages, three input sources:

1. **Capture.** Claude Code writes via `PostToolUse` hook in real time. Cursor + Codex CLI are ingested hourly by the auto-migrate LaunchAgent (per-tool adapters parse their native formats — `*.plan.md` for Cursor, `rollout-*.jsonl` for Codex sessions). Each source feeds the same `~/.agent/memory/episodic/` (Codex episodes land under `episodic/codex/` for namespace isolation).
2. **Distill (nightly).** `auto_dream.py` clusters episodes by salience and promotes high-signal patterns to `~/.agent/memory/candidates/`. Atomic writes; no torn-file windows.
3. **Graduate (your review).** `agent/tools/graduate.py <id>` promotes a candidate to `~/.agent/memory/semantic/` (permanent). `reject.py` discards. `MEMORY.md` index updates so the next session loads it automatically.
4. **Sync (hourly).** `sync.sh` runs `trufflehog`/`gitleaks`, scrubs episodic JSONL with `redact_jsonl.py`, then `git push` to your private brain remote. Override audit log + a server-side GitHub Action catch local bypasses.
5. **Recall (next session).** `MEMORY.md` auto-loads via `CLAUDE.md`. Past ~150 lessons, use the [`recall`](recall/README.md) CLI/MCP for hybrid BM25 + embedding retrieval.

The loop closes daily. Each session writes new episodes; each night the dream cycle distills them; each morning the agent reads back the distilled lessons. Git sync runs orthogonally to the loop, so nothing on disk is ever the only copy.

---

## Architecture

```
~/.agent/
├── memory/
│   ├── working/         # ephemeral session state, REVIEW_QUEUE.md
│   ├── episodic/        # AGENT_LEARNINGS.jsonl + .lock sentinel
│   ├── semantic/        # graduated lessons (lessons.jsonl + LESSONS.md)
│   ├── personal/        # profile, preferences, references, notes
│   ├── candidates/      # staged by dream cycle, awaiting your review
│   ├── _atomic.py       # temp+fsync+os.replace helpers
│   ├── auto_dream.py    # nightly clustering pass
│   └── MEMORY.md        # human-readable index, auto-loaded by CLAUDE.md
├── tools/               # redact, scrub, dream_runner, sync, graduate/reject
├── harness/hooks/       # PostToolUse capture (validated brain root)
├── redact-private.txt   # YOUR org-specific patterns
├── override.log         # audit trail of .agent-local-override fires
└── .git/                # pushed to your private GitHub remote
```

Full architecture in [`docs/architecture.md`](docs/architecture.md). Memory model in [`docs/memory-model.md`](docs/memory-model.md). Dream cycle internals in [`docs/dream-cycle.md`](docs/dream-cycle.md).

---

## Security

The brain holds tool-call history including raw Bash and Edit deltas — pushing that to a remote without guardrails is a credential leak waiting to happen. Five layers of defense:

1. **Pre-commit `redact.py`** — 16 vendor token patterns (AWS, GitHub, Anthropic, Slack, Stripe, …) + URL-aware Shannon entropy sweep.
2. **`redact-private.txt`** — your org-specific patterns. ReDoS-prone regexes rejected at load.
3. **Sync-time `redact_jsonl.py`** — recursive scrubber over every string field in episodic JSONL.
4. **Required scanner at sync** — `sync.sh` refuses to push without `trufflehog` or `gitleaks`.
5. **Server-side GitHub Action** — re-runs both scanners on push/PR, catching `--no-verify` bypasses.

Plus structural hardening: validated `BRAIN_ROOT` (rejects paths outside `$HOME`), sentinel-locked atomic writes (verified 0/800 lost rows under 20-way contention), override audit log, identity-scrubbing migrator. Full threat model in [`docs/redaction-policy.md`](docs/redaction-policy.md).

---

## Retrieval

Past ~150 lessons, the auto-loaded `MEMORY.md` index alone stops being enough. [`recall`](recall/README.md) is the read-side companion — zero-config CLI + MCP server doing hybrid BM25 + embedding retrieval over `$BRAIN_ROOT/memory/`.

```bash
pip install -e '.[embeddings,mcp]'
recall reindex
recall query "how do I avoid context bloat from reading too many files"
```

Quality numbers (synthetic-corpus, deterministic seed) and per-strategy benchmark in [`recall/README.md`](recall/README.md). PRs touching `recall/` gate on a 5pp recall@5 tolerance.

---

## What's shipped

**Storage + retrieval (v0.1):**
- **Multi-tool ingest.** Claude Code (real-time via symlink), Cursor plans, Codex CLI sessions. Pluggable `Adapter` Protocol — see [`docs/multi-tool-migrate.md`](docs/multi-tool-migrate.md) for authoring a new one.
- **Auto-migrate LaunchAgent.** `./install.sh --setup-auto-migrate` — sets it once, forget it.
- **Discovery + interactive wizard.** `./install.sh --migrate` (no source) auto-detects what's on disk and lets you pick.
- **Hybrid retrieval.** Qdrant + BM25 over your global brain.

**Persistent brain edits (v0.4):**
- **`recall remember "..."`** writes a markdown lesson to `~/.agent/memory/semantic/lessons/` with frontmatter. Auto-loaded forever. `--as <slug>` for explicit names; `--overwrite` for replace.
- **`recall forget <query>`** archives a lesson (basename / substring match) to `~/.agent/memory/semantic/archived/<ts>-<name>.md`. Recoverable with `mv`. Multi-match lists candidates.

**Context runtime (v0.2 → v0.4):**
- **Manifest + event log schemas.** Versioned (v1.1), deterministic byte-identical round-trip, byte-equal replay vs live engine.
- **Engine state machine.** Token budgets per bucket; pluggable eviction policy (LRU, recency-weighted, pinned-first ship as defaults; you write your own as a Python file).
- **`recall runtime`** subcommand group: `ls`, `timeline` (flight-recorder summary + `--full`), `tail` (recent events plain English), `replay [--diff A:B]`, `add <file-or-text>` (resolves natural queries; supports inline text), `evict <query> [--intent]`, `pin`/`unpin`, `budget`, `install-hooks`.
- **Re-injection (opt-in).** `enable_reinjection = true` activates a `UserPromptSubmit` hook that prepends a delimited block of pinned + user-added + user-evicted items to the next prompt. Hard token budget; clear delimiters; no vendor magic.
- **Reference-only by default.** No raw tool output in default-on artifacts; `capture_raw = true` opt-in goes to a separate git-ignored file.
- **Host-agnostic core.** `runtime/core/` has zero Claude-specific imports. Cursor and Codex CLI adapters drop in alongside.
- **`recall` on PATH automatically.** `install.sh` symlinks `~/.local/bin/recall` after pip-install into a venv.

## Roadmap

**Runtime (next minor versions):**
- **PostCompact-driven re-injection.** Today the runtime records compaction events but doesn't auto-recover from them. The fix: on `PostCompact`, replay the last N turns and prepend a "minimum viable refresh" block to the next prompt. Closes the compaction-amnesia loop without user intervention.
- **HMAC-keyed `OutputSummary`.** sha256 is currently a stable fingerprint of tool output (defaults to empty for safety). v0.x adds a per-session HMAC key so users who want correlation across turns can opt in without exposing fingerprints to a breach-DB-style correlator.
- **Multi-version schema reader.** v1.1 logs become unreadable when the next bump lands. Loaders should accept a range of versions to make schema upgrades non-breaking.
- **Citation-feedback loop.** Parse responses for which chunks the model actually cited; auto-demote uncited retrievals so retrieval scoring improves with use.
- **Cross-context contradiction detection.** When two items in the active manifest assert opposite facts, surface a warning before they reach the model.

**Adapters:**
- **Cursor + Codex CLI hook adapters** at the runtime layer (storage already supports both). Each host has a different hook lifecycle; separate PR per host.
- **Aider, Cline, Windsurf, Continue adapters** when those tools' data shows up on a real user's machine.

**Brain ergonomics:**
- **Per-namespace clusterer** so Codex episodes graduate to lessons (today they ingest but stay in `episodic/codex/` because the default clusterer is namespace-default-only).
- **Brain visualization dashboard.** Today the brain is greppable text; a small read-only UI would help see lesson clusters at a glance.
- **Active-recall verification** for high-value lessons (does Claude actually apply this? how often?).

**Distribution:**
- Brew tap so `brew install brainstack` puts `recall` on PATH without the venv shuffle.
- Linux systemd-timer port (currently macOS launchd only).

**Compounding intelligence (later):**
- Opt-in cross-user lesson sharing (auto-redacted).
- Cross-project retrieval — "when working on repos like this, you previously learned X."

Throughline: keep the security posture sharp. The framework's value is proportional to how much you trust putting your tool-call history into a remote.

---

<details>
<summary>How this differs from upstream agentic-stack</summary>

We vendor 20 files from [codejunkie99/agentic-stack](https://github.com/codejunkie99/agentic-stack) verbatim (clustering, decay, lesson rendering — see [`UPSTREAM.md`](UPSTREAM.md)). Different design point:

| | upstream | brainstack |
|---|---|---|
| Brain location | per-project `.agent/` | one global `~/.agent/` |
| Multi-machine | not designed for | `git pull` on session start |
| Laptop-loss durability | local disk only | mirrored to private git remote |
| Secret redaction | basic | 5-layer defense |
| Atomic-write safety | basic | sentinel-locked; verified under stress |

Per-project brains fragment context — you relearn the same lesson 10 times across 10 repos. Global persistence + git sync turns the same engine into a substrate that compounds.

</details>

---

## License

Apache 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE) for upstream attribution.

This is personal infrastructure shared as-is. Issues and PRs welcome but no support obligations are implied.
