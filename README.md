# brainstack

**A persistent, git-synced brain for your AI coding agent — with a runtime that records, budgets, and replays what enters your agent's context each turn.**

Three layers, one stack:

- **Storage.** One global memory at `~/.agent/`. Every tool call → episodic log → nightly dream cycle clusters salient patterns → graduated lessons land in `semantic/` and are auto-loaded on every future session. Mistakes get codified so the next session reuses them.
- **Retrieval.** Hybrid recall (Qdrant + BM25) finds the right memory for any query. Tool-agnostic: works with Claude Code, Cursor, Codex CLI.
- **Runtime.** Token budgets per bucket, eviction policy as a forkable Python file, full replay/audit. Answers *"why didn't the model know X?"* from artifacts instead of guesswork. The runtime core is host-agnostic; v0.4 ships with the first adapter, Claude Code. Planned Cursor / Codex CLI adapters slot into the same interface (community contributions welcome). Today the runtime *records, budgets, and replays* every injection decision a Claude Code session makes; it does not yet inject CLAUDE.md content on its own (that lands in a later minor). What it controls is the log, the manifest, and the replay — enough to debug and prove behavior.

**Constant git sync.** Hourly push to your private remote (with required secret-scanner gate). Reinstall on a new machine and `git pull` brings back every lesson, every preference, every reference. Details: [`docs/git-sync.md`](docs/git-sync.md).

**One brain, every project.** Global `~/.agent/` (not per-repo), so a lesson learned debugging Postgres in repo A is available the next time you touch Postgres in repo Z.

The model gets smarter every release. Your agent only gets smarter if its context does. This is the substrate for that.

Built on top of [codejunkie99/agentic-stack](https://github.com/codejunkie99/agentic-stack) — vendored dream cycle, clustering, lesson rendering. See [`UPSTREAM.md`](UPSTREAM.md).

---

## Quickstart

**Prereqs:** `git`, Python 3.10+ (for the venv `install.sh` builds), an existing private git remote you control (used as your brain's mirror), and Claude Code if you want the runtime hooks. macOS or Linux.

```bash
git clone https://github.com/mustafa-bharmal-shipveho/brainstack.git
cd brainstack

# One-shot install. Creates ~/.agent/, wires it as a git repo with YOUR
# private remote, makes the initial commit, installs the pre-commit
# redaction hook, sets up a venv, pip-installs `recall`, symlinks it
# into ~/.local/bin/.
./install.sh --brain-remote git@github.com:<you>/<your-private-brain-repo>.git \
             --push-initial-commit

# Make sure ~/.local/bin is on $PATH (installer prints a one-line export tip
# if it isn't), then wire the runtime hooks. This step IS allowed to edit
# ~/.claude/settings.json — idempotently, preserving any existing hooks.
recall runtime install-hooks
```

`install.sh` itself never touches `~/.claude/` (that's a separate decision via `recall runtime install-hooks`). Setup details: [`docs/claude-code-setup.md`](docs/claude-code-setup.md). After PATH is set, `recall remember`, `recall forget`, `recall query`, and `recall runtime *` work as bare commands.

Migrating from existing AI-tool memory dirs (Claude Code, Cursor, Codex CLI):

```bash
./install.sh --migrate                        # interactive — discovers + lets you pick
./install.sh --migrate ~/.claude/projects/<slug>/memory   # explicit source
./install.sh --setup-auto-migrate              # one-time wizard, then forget it
```

---

## What you can do with it

| Command | Scope | Use it for |
|---|---|---|
| `recall query "..."` | global brain | Hybrid BM25 + embedding search across every lesson + episode |
| `recall remember "always use /agent-team"` | **forever** | Lessons Claude should know on every future session |
| `recall forget agent-team` | **forever** | Archives a lesson back out (recoverable from `archived/`) |
| `recall runtime add <file-or-text>` | one prompt | Push a file or inline text into the next prompt's re-injection block |
| `recall runtime evict <query>` | current session | Demote-from-injection on the next turn |
| `recall runtime timeline` | current session | Flight-recorder digest of what entered + left the manifest |

All commands accept natural queries (path, basename, substring, id-prefix); no cryptic IDs to copy-paste. Beyond these, the brain quietly runs its **nightly dream cycle** — clustering high-signal patterns from your tool-call history, staging candidate lessons for your review, graduating the ones you keep, git-syncing the result hourly. See [`docs/dream-cycle.md`](docs/dream-cycle.md).

More inspection: `recall runtime ls`, `recall runtime tail`, `recall runtime replay --diff A:B`, `recall runtime budget`, `recall runtime pin / unpin`.

---

## Edit your second brain in one command

```bash
recall remember "always use /agent-team for development"
recall remember "use SELECT FOR UPDATE SKIP LOCKED for queue claims" --as postgres-locking
recall forget agent-team
```

Lessons land in `~/.agent/memory/semantic/lessons/<slug>.md`, brainstack auto-loads them on every future session. `recall forget` archives by name or substring (multi-match lists candidates).

That's the "I keep telling Claude X every session and it forgets" loop, closed in one line.

---

## Flight-recorder summary of every session

```text
$ recall runtime timeline
Flight recorder for session "current" — 9 turns, 173 events.

112 items entered the manifest this session.
78 were evicted on bucket overflow (40 budget breaches).
34 items are still in the manifest.

Recent budget breaches:
  • turn 1 Read: evicted 5 items [c-02569426, c-08a64eec, c-0c8e88a1, c-16c0448f + 1 more]
  • turn 1 Bash: evicted 1 item [c-18c80ceb]
  • …and 35 more (run with --full to see every event)

Manifest now:
  retrieved    33 items  19207 / 20000 tokens (96% full)
  scratchpad    1 item    7911 / 10000 tokens (79% full)
```

12 lines instead of 150. Tells you where injection pressure came from so you can raise budgets, pin important items, or change what files you read. `--full` for the chronological firehose.

---

## "Why did Claude forget X?" — answered from artifacts

Forty turns into a Postgres-deadlock debug session, Claude proposes a fix that locks the whole table — even though you taught it `SELECT FOR UPDATE SKIP LOCKED` at turn 6.

```text
$ recall runtime replay --diff 37:38

evicted (1):
  - c-a3f0294b1c    (retrieved   280 tok)  retrieved/turn-6-fix-summary.md
added (1):
  + c-77ab19d34e    (retrieved   412 tok)  retrieved/postgres-locking-survey.md
```

The turn-6 fix dropped out of the injection set at turn 38 — LRU demoted it after a compaction rebuilt the warm tier. Two-line policy change pins it next time. The distinction worth highlighting: brainstack ships **user-readable replay artifacts for this transition**. mem0 stores facts; claude-obsidian writes a recap; Letta pages internally. None hands you a diff between turn 37's manifest and turn 38's that you can grep, copy into a PR, or use to argue with your policy.

---

## Context runtime — the layer doing all this

Three layers under the hood:

- **Storage** (`~/.agent/`) — every tool call → episodic log → graduated lessons.
- **Retrieval** (`recall query`) — Qdrant + BM25 hybrid over your global brain.
- **Runtime** (`runtime/`) — manifest + budgets + eviction policy + replay + Claude Code adapter.

The runtime is **host-agnostic at the core** (`runtime/core/` has zero Claude-specific imports); the Claude Code wiring lives in `runtime/adapters/claude_code/`. Planned Cursor and Codex CLI adapters can slot into the same interface — community contributions welcome.

**Honest boundary:** the runtime owns the *injection layer* (what we push through hooks, CLAUDE.md, and `UserPromptSubmit` re-injection). It does not own the model's KV cache or accumulated conversation history — those are opaque to any tool. "Eviction" means "demotion-from-injection on the next turn."

Full design + schema reference + opt-in re-injection mechanism + configuration: [`docs/runtime.md`](docs/runtime.md).

### What it isn't

- Not a vault ([claude-obsidian](https://github.com/AgriciDaniel/claude-obsidian) does that)
- Not a vector store ([mem0](https://mem0.ai), [Zep](https://www.getzep.com), [Cognee](https://cognee.ai))
- Not an agent framework ([Letta / MemGPT](https://www.letta.com))
- Not observability ([Helicone](https://helicone.ai), [Langfuse](https://langfuse.com), [LangSmith](https://smith.langchain.com))
- Not a UI — CLI + JSON only

The layer above retrieval, below the agent loop, owning the budget + eviction + audit. That layer was empty before brainstack v0.4.

---

## Set it once, forget it

```bash
./install.sh --setup-auto-migrate
```

Wizard asks which tools you use, installs ONE LaunchAgent that runs every hour and ingests new Cursor plans + Codex CLI sessions into the brain — sub-second incremental runs via offset-tracked idempotency. Claude Code is already automatic via the symlink. After this runs, you don't have to remember to migrate anything again.

For Claude Code's native auto-memory, `--migrate` defaults to swapping the source for a symlink to `~/.agent/memory` (with a timestamped backup) — so Claude Code's ongoing writes flow into the brain in real time. Cursor + Codex sources are ingested as snapshots (those tools keep writing to their own dirs).

Power users can drive it non-interactively: `--enable cursor-plans,codex-cli`, `--all`, `--none`, `--dry-run`, `--print-plist`. Tear-down: `./install.sh --remove-auto-migrate`. Health check: `./install.sh --verify` or `make report-status`.

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
- **Multi-tool ingest.** Claude Code (real-time via symlink), Cursor plans, Codex CLI sessions. Pluggable `Adapter` Protocol; new adapters slot into `agent/adapters/`.
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
- **Host-agnostic core.** `runtime/core/` has zero Claude-specific imports. Cursor and Codex CLI adapters are planned and can slot into the same interface.
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
