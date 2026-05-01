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

## Storage: capture, distill, graduate

```
                                capture                distill                graduate              recall
   ┌─ Claude Code (real-time hook) ──────────► episodic/ ──────────► candidates/ ──────────► semantic/ ────► next session
   ├─ Cursor (hourly LaunchAgent) ─────────────►   JSONL log,         staged by              you review        auto-loaded
   └─ Codex CLI (hourly LaunchAgent) ──────────►   sentinel-locked    dream cycle            graduate.py /     via MEMORY.md
                                                                      (nightly)              reject.py         → CLAUDE.md

                                              ↻ git sync (hourly) — push to your private brain remote, scanner-gated
```

**Five stages, three input sources:**

1. **Capture.** Claude Code writes via `PostToolUse` hook in real time. Cursor + Codex CLI ingest hourly via the auto-migrate LaunchAgent. All three feed the same `~/.agent/memory/episodic/`.
2. **Distill (nightly).** `auto_dream.py` clusters episodes by salience; high-signal patterns become candidates.
3. **Graduate (your review).** `graduate.py <id>` promotes a candidate to permanent `semantic/`; `reject.py` discards.
4. **Sync (hourly).** `sync.sh` runs `trufflehog`/`gitleaks`, scrubs JSONL, pushes to your private brain remote.
5. **Recall (next session).** `MEMORY.md` auto-loads via `CLAUDE.md`. Beyond ~150 lessons, use the `recall` CLI / MCP.

Edit the brain directly when you don't want to wait for the dream cycle:

```bash
recall remember "always use /agent-team for development"
recall remember "use SELECT FOR UPDATE SKIP LOCKED for queue claims" --as postgres-locking
recall forget agent-team    # archives by name or substring; multi-match lists candidates
```

Lessons land in `~/.agent/memory/semantic/lessons/<slug>.md` and auto-load on every future session, forever.

Set-and-forget multi-tool ingest: `./install.sh --setup-auto-migrate` installs a single hourly LaunchAgent that pulls Cursor + Codex CLI sessions in alongside Claude Code's real-time symlink. `--enable`, `--all`, `--dry-run` for non-interactive runs; `--remove-auto-migrate` to tear down. Details: [`docs/dream-cycle.md`](docs/dream-cycle.md), [`docs/git-sync.md`](docs/git-sync.md), [`docs/memory-model.md`](docs/memory-model.md).

---

## Retrieval: hybrid search across the global brain

```bash
pip install -e '.[embeddings,mcp]'
recall reindex
recall query "how do I avoid context bloat from reading too many files"
```

Qdrant + BM25 hybrid over `$BRAIN_ROOT/memory/`. Tool-agnostic — works as a CLI, an MCP server, or a Python import; callable from Claude Code, Cursor, Codex CLI, or your own scripts. Quality numbers (synthetic-corpus, deterministic seed) and per-strategy benchmark in [`recall/README.md`](recall/README.md). PRs touching `recall/` gate on a 5pp recall@5 tolerance.

---

## Runtime: what's in the context window this turn

The runtime owns the **injection layer**: what is pushed through hooks, `CLAUDE.md`, and `UserPromptSubmit` re-injection. Manifest + token budgets + eviction policy + replay. Host-agnostic core (`runtime/core/`) plus a Claude Code adapter (`runtime/adapters/claude_code/`); planned Cursor + Codex CLI adapters slot into the same interface. Wire it once with `recall runtime install-hooks`, then it runs silently.

```bash
$ recall runtime timeline
Flight recorder for session — 9 turns, 173 events.
112 items entered the manifest. 78 were evicted on bucket overflow. 34 still in.
retrieved 96% full · scratchpad 79% full · run with --full for the firehose

$ recall runtime replay --diff 37:38
evicted (1):  - c-a3f0294b1c   (retrieved  280 tok)  retrieved/turn-6-fix-summary.md
added   (1):  + c-77ab19d34e   (retrieved  412 tok)  retrieved/postgres-locking-survey.md

$ recall runtime add ~/.agent/memory/semantic/lessons/postgres-locking.md   # file
$ recall runtime add "use /agent-team for this debugging session"           # inline text
$ recall runtime evict postgres-locking                                     # natural query, no IDs
```

Answers *"why did Claude forget X at turn 38?"* from artifacts: LRU demoted the turn-6 fix when compaction rebuilt the warm tier. Pin it, or change the policy file (one small Python module at `runtime/core/policy/lru.py` — read it, fork it, version it).

**Honest boundary:** "eviction" here means demotion-from-the-next-injection, not eviction from the model's KV cache (which is opaque to any tool). What's manifestable, budgetable, and replayable is what we push in — that's the layer brainstack owns. Full design + schema + opt-in re-injection mechanism: [`docs/runtime.md`](docs/runtime.md).

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

## Roadmap

- **Runtime:** PostCompact-driven re-injection (auto-recovery from compaction); HMAC-keyed `OutputSummary` for safe cross-turn correlation; multi-version schema reader; citation-feedback loop; cross-context contradiction detection.
- **Adapters:** Cursor + Codex CLI runtime hooks (storage already supports both); Aider / Cline / Windsurf / Continue when their data shows up on a real user's machine.
- **Brain ergonomics:** per-namespace clusterer so Codex episodes graduate to lessons; lightweight read-only dashboard; active-recall verification for high-value lessons.
- **Distribution:** Brew tap; Linux systemd-timer port (currently macOS launchd only).
- **Compounding intelligence (later):** opt-in cross-user lesson sharing (auto-redacted); cross-project retrieval.

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
