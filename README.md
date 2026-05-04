# brainstack

**A persistent, git-synced brain for your AI coding agent — with a runtime that records, budgets, and replays what enters your agent's context each turn.**

The core question brainstack measures is simple:

> **Did the agent have the right memory/context when it mattered?**

Everything else is instrumentation for that answer: durable capture, retrieval quality, token budgets, eviction policy, and replay.

Three layers, one stack:

- **Storage.** One global memory at `~/.agent/`. Every tool call → episodic log → nightly dream cycle clusters salient patterns → graduated lessons land in `semantic/` and are auto-loaded on every future session. Mistakes get codified so the next session reuses them.
- **Retrieval.** Hybrid recall (Qdrant + BM25) finds the right memory for any query. Tool-agnostic: works with Claude Code, Cursor, Codex CLI.
- **Runtime.** Token budgets per bucket, eviction policy as a forkable Python file, full replay/audit. Answers *"why didn't the model know X?"* from artifacts instead of guesswork. The runtime core is host-agnostic; v0.4 ships with the first adapter, Claude Code. Planned Cursor / Codex CLI adapters slot into the same interface (community contributions welcome). Today the runtime *records, budgets, and replays* every injection decision a Claude Code session makes; it does not yet inject CLAUDE.md content on its own (that lands in a later minor). What it controls is the log, the manifest, and the replay — enough to debug and prove behavior.

**Constant git sync.** Hourly push to your private remote (with required secret-scanner gate). Reinstall on a new machine and `git pull` brings back every lesson, every preference, every reference. Details: [`docs/git-sync.md`](docs/git-sync.md).

**One brain, every project.** Global `~/.agent/` (not per-repo), so a lesson learned debugging Postgres in repo A is available the next time you touch Postgres in repo Z.

The model gets smarter every release. Your agent only gets smarter if its context does. This is the substrate for that.

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
# ~/.claude/settings.json — idempotently, preserving any existing hooks and
# non-hook keys (theme, permissions, etc.). The file is rewritten with
# indent=2 + sort_keys=True, so whitespace and key order may change; data
# is preserved. Re-runs are a no-op (entries marked with a
# `# brainstack-runtime` sentinel).
recall runtime install-hooks
```

`install.sh` itself never touches `~/.claude/` (that's a separate decision via `recall runtime install-hooks`). Setup details: [`docs/claude-code-setup.md`](docs/claude-code-setup.md). After PATH is set, `recall remember`, `recall forget`, `recall query`, and `recall runtime *` work as bare commands.

**Bringing existing memories in (recommended).** A fresh `./install.sh` creates an empty `~/.agent/`. Your existing Claude Code, Cursor, and Codex CLI memories are not auto-imported. The simplest set-and-forget option installs one hourly LaunchAgent that pulls all three tools' new entries into the brain forever:

```bash
./install.sh --setup-auto-migrate              # recommended — interactive: pick which tools
```

Or do a one-time snapshot import (Claude Code dirs are also swapped for a symlink so real-time writes keep flowing in; the original is preserved at `<source>.bak.<ts>`. Cursor and Codex CLI imports are snapshot-only — new entries after this won't reach the brain unless you re-run migrate or use `--setup-auto-migrate`):

```bash
./install.sh --migrate                                    # interactive — discovers + lets you pick
./install.sh --migrate ~/.claude/projects/<slug>/memory   # explicit source
```

Tear down the LaunchAgent later with `./install.sh --remove-auto-migrate`.

**Optional: deeper Claude Code mirroring** — `--setup-auto-migrate` covers Cursor + Codex, but doesn't pull Claude's per-session transcripts (`~/.claude/projects/<slug>/*.jsonl`) or the misc dirs Claude writes to (`plans/`, `tasks/`, `sessions/`, `agents/`, etc.). To capture those too without touching the source dirs:

```bash
./install.sh --setup-claude-extras    # installs com.brainstack.claude-extras LaunchAgent
```

This adds an hourly LaunchAgent that runs two adapters under the same fcntl lock as auto-migrate-all:

| Adapter | Source | Lands in |
|---|---|---|
| `claude_session_adapter.py` | `~/.claude/projects/<slug>/*.jsonl` (top-level + subagent transcripts) | `~/.agent/memory/episodic/claude-sessions/AGENT_LEARNINGS.jsonl` (one episode per `tool_use`/`tool_result` pair) |
| `claude_misc_adapter.py` | `~/.claude/{plans,tasks,sessions,teams,agents,skills,CLAUDE.md}` + every `~/.claude/projects/<slug>/memory/` not already symlinked + `~/.cursor/skills-cursor/` | `~/.agent/imports/<tool>/...` (mirror, mtime-incremental) |

**Mirror, don't swap.** Unlike `--migrate`, these adapters never modify the source — Claude Code keeps writing to its own folders, and brainstack pulls from there into `~/.agent` on a schedule. Both adapters use SHA-256 / mtime sidecars so re-runs are O(N) stat-only no-ops.

Excluded by policy (privacy/volume): `~/.claude/{history.jsonl,paste-cache,file-history,telemetry}` (clipboard pastes / file backups / telemetry — not memory) and `~/.cursor/ai-tracking` (opaque SQLite blob with high-entropy hits). Audit live coverage with `~/.agent/tools/discover_all_sources.py`.

Tear down with `./install.sh --remove-claude-extras`.

---

## Upgrading after a `git pull`

`install.sh` seeds `~/.agent/{tools,memory,harness}/` from the repo at
install time. After that, **`git pull` of brainstack does NOT propagate
those updates** — `~/.agent/` keeps running whatever framework code was
installed originally. Two real-world bugs from forgetting this:

- `~/.agent/tools/auto_migrate_install.py` missing → `--setup-auto-migrate`
  fails with `tools/...py is missing`
- `~/.agent/memory/auto_dream.py` stale → dream cycle silently skips
  whole episodic namespaces

Refresh runtime code without touching user data (episodic, candidates,
semantic, personal) with:

```bash
cd /path/to/brainstack && ./install.sh --upgrade
```

The upgrade is idempotent and rsync-with-`--delete`, so it adds new
tools, replaces stale ones, and removes upstream-deleted ones — while
preserving any `*.user.*` helper scripts you've added locally and every
file under `~/.agent/memory/{episodic,candidates,semantic,personal,working}/`.

**Drift detection.** Re-running plain `./install.sh` on an existing brain
prints a one-line warning if the brain is out of sync with the repo, and
the LaunchAgent entry points (`dream_runner.py`, `migrate_dispatcher.py
auto-migrate-all`) emit the same warning to stderr / their log files on
every tick. To audit on demand:

```bash
~/.agent/tools/check_freshness.py            # human report; exits non-zero if drift
~/.agent/tools/check_freshness.py --json     # machine-parseable
```

---

## Knowing what to review

The dream cycle clusters episodes into candidate lessons in `~/.agent/memory/candidates/`. Until you triage them (graduate or reject), they sit there silently. Without surfacing, real work piles up: on the maintainer's brain on 2026-05-04, **21 candidates had been pending for 3 days before anyone noticed**.

Brainstack now generates `~/.agent/PENDING_REVIEW.md` on every dream cycle, sync, graduate, or reject — and surfaces it through three native injection points so you see the count whenever you start a session:

| Surface | Setup | Where you see it |
|---|---|---|
| Claude Code SessionStart hook | merge `adapters/claude-code/settings.snippet.json` SessionStart entry into `~/.claude/settings.json` | Top of every Claude Code session, in a `<system-reminder>` block |
| Cursor `~/.cursor/.cursorrules` | `./install.sh --setup-cursor-rules` | Cursor injects the rules file on every chat session |
| Shell wrappers (`claude`/`codex`/`cursor`) | `./install.sh --setup-shell-banner` | Cat'd to stdout when you launch any of those tools from a terminal |

All three read the same `PENDING_REVIEW.md`. The file is generated locally and gitignored — never synced to your private brain remote.

**Framework, not point-solution.** The shell banner wraps any AI CLI listed in `~/.agent/banner/wrapped_tools` — one tool name per line, `#` comments allowed. Default set covers `claude`, `codex`, `cursor`, `aider`, `continue`, `gemini`, `ollama`, `llm`. Adding a new LLM is a one-line edit; re-source `~/.zshrc` to apply. No code change. The Cursor `.cursorrules` and Claude SessionStart paths similarly rely on each tool's own native injection mechanism — when a new AI tool ships hooks/rules support, brainstack adds an adapter rather than reinventing the surface.

```bash
# Add a new AI CLI to the shell wrappers
echo "my-new-llm" >> ~/.agent/banner/wrapped_tools
source ~/.zshrc                              # re-source to pick it up
type my-new-llm                              # confirm wrapper defined
```

One-shot setup of all three surfaces:

```bash
./install.sh --setup-pending-review-all      # Claude SessionStart + Cursor + shell wrappers
./install.sh --remove-pending-review-all     # tear down all three
```

```bash
# View the summary on demand
recall pending                  # print current summary
recall pending --refresh        # force regenerate first
recall pending --review         # interactive triage flow

# Manual regeneration (also runs automatically on dream/graduate/reject/sync)
python ~/.agent/tools/render_pending_summary.py
```

The summary includes:
- pending candidate counts per namespace (default / claude-sessions / codex)
- top 5 candidates by signal (after a noise filter that rejects test-infra clusters — `/tmp/`, sandbox paths, `FAILED (secret)` patterns)
- drift status (via `check_freshness`)
- sync staleness (sync.log mtime > 2h or last-line "refusing to push")

Empty days produce a one-liner (`✅ all clear`) which all three surfaces suppress, so a healthy brain produces zero session-start noise.

### Tearing it down

```bash
./install.sh --remove-cursor-rules     # strips sentinel block from .cursorrules
./install.sh --remove-shell-banner     # strips source line from ~/.zshrc + removes script
# For the SessionStart hook: edit ~/.claude/settings.json and remove the entry
```

### Security note

The SessionStart hook injects `PENDING_REVIEW.md` content into Claude Code's context inside a `<system-reminder>` block. To prevent a project-level `.envrc` from poisoning `$HOME` or `$BRAIN_ROOT` and redirecting the hook to attacker-controlled content, the hook resolves the brain root from `__file__` (its own install path), not from environment variables. Tests pin this in `tests/test_render_pending.py::test_resolves_brain_from_file_not_env`.

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

**Four stages, three input sources:**

1. **Capture.** Claude Code writes via `PostToolUse` hook in real time. Cursor + Codex CLI ingest hourly via the auto-migrate LaunchAgent. All three feed the same `~/.agent/memory/episodic/`.
2. **Distill (nightly).** `auto_dream.py` clusters episodes by salience; high-signal patterns become candidates.
3. **Graduate (your review).** `graduate.py <id>` promotes a candidate to permanent `semantic/`; `reject.py` discards.
4. **Sync (hourly).** `sync.sh` runs `trufflehog`/`gitleaks`, scrubs JSONL, pushes to your private brain remote.

**Where new memories land between sessions:**

| Tool | Native write path | Reaches `~/.agent/` via |
|---|---|---|
| Claude Code | symlinked from `~/.claude/projects/<slug>/memory` | direct write (symlink) + real-time `PostToolUse` hook |
| Cursor | `~/.cursor/plans/*.plan.md` | hourly `--setup-auto-migrate` LaunchAgent |
| Codex CLI | `~/.codex/sessions/...`, `~/.codex/history.jsonl` | hourly `--setup-auto-migrate` LaunchAgent |

So Claude Code memory is in the brain immediately; Cursor and Codex entries arrive on the next hourly tick. All three end up in the same `~/.agent/memory/` tree and feed the same nightly dream cycle.

### How we measure storage reliability

Storage quality is measured by two questions: did the captured row survive, and did secret-bearing tool output get scrubbed before sync?

```bash
python3 -m pytest tests/test_concurrent_appends.py tests/test_pipeline_e2e.py -q
```

The concurrency gate runs **20 appenders x 5 rows** while the dream cycle rewrites the episodic log. Expected result: **100/100 rows survive, 0 lost**, either in the live JSONL or dream-cycle snapshots. That test exists because locking the data file directly previously caused silent loss under `os.replace`; sentinel locking is now the contract.

The redaction pipeline test injects fake AWS/Bearer/Edit secrets into captured tool-call JSONL, runs the JSONL scrubber, then runs the scanner. Expected result: **0 literal secrets remain** and `[REDACTED:...]` markers replace them. A plain auto-memory folder usually has no append-concurrency gate and no scrub-then-scan sync gate.

Set-and-forget multi-tool ingest: `./install.sh --setup-auto-migrate` installs a single hourly LaunchAgent that pulls Cursor + Codex CLI sessions in alongside Claude Code's real-time symlink. `--enable`, `--all`, `--dry-run` for non-interactive runs; `--remove-auto-migrate` to tear down. Details: [`docs/dream-cycle.md`](docs/dream-cycle.md), [`docs/git-sync.md`](docs/git-sync.md), [`docs/memory-model.md`](docs/memory-model.md).

Built on top of [codejunkie99/agentic-stack](https://github.com/codejunkie99/agentic-stack) — vendored dream cycle, clustering, lesson rendering. See [`UPSTREAM.md`](UPSTREAM.md).

---

## Retrieval: how you read and edit the brain

Next session, `MEMORY.md` auto-loads via `CLAUDE.md`. Beyond ~150 lessons that index alone stops being enough, so the `recall` CLI / MCP server takes over with hybrid Qdrant + BM25 search over `$BRAIN_ROOT/memory/`.

```bash
pip install -e '.[embeddings,mcp]'
recall reindex
recall query "how do I avoid context bloat from reading too many files"
```

Edit the brain directly when you don't want to wait for the dream cycle to graduate something:

```bash
recall remember "always use /agent-team for development"
recall remember "use SELECT FOR UPDATE SKIP LOCKED for queue claims" --as postgres-locking
recall forget agent-team    # archives by name or substring; multi-match lists candidates
```

`recall remember` writes a markdown lesson to `~/.agent/memory/semantic/lessons/<slug>.md`; it auto-loads on every future session, forever. `recall forget` moves it to `archived/` (recoverable). Both accept natural queries — no cryptic IDs to copy-paste.

### How we measure retrieval quality

Retrieval quality is measured as **recall@5**: when a user asks a question, does the right memory, or the right family of memories, appear in the top five results?

Two benchmarks cover different failure modes:

```bash
# Public synthetic benchmark: compares recall vs no-recall at 80 / 1k / 5k memories.
python tests/recall/bench_e2e.py --scale 5000 --report

# Local power-user benchmark: seeds a large private simulation from your own memory.
python tests/recall/bench_memory_quality.py --simulate-target-docs 5000
```

The no-Brainstack baseline is what most agents do by default: rely on a static `MEMORY.md` / `CLAUDE.md` index, often truncated by the host, or manually read files after the model already guesses what matters. At 5,000 synthetic lessons, that path gets about **12% paraphrase recall@5** when the index is truncated and about **35%** even with the full index. Brainstack hybrid recall is about **90% paraphrase recall@5** at the same size. Full numbers are in [`tests/recall/BENCH_RESULTS.md`](tests/recall/BENCH_RESULTS.md).

The local power-user simulation answers a different question: if this brain grew to about 5,000 memories, would the current budget shape still surface the right memory? On the current memory style, the expected memory family appeared in the top five **100%** of the time; 4,000 retrieved tokens included it **98%** of the time, and 20,000 retrieved tokens included it **100%** of the time. That suggests the current `retrieved = 20000` budget is enough for memory retrieval; the harder problem is session tool-output churn, which the runtime measures separately with eviction/replay.

Tool-agnostic — runs as CLI, MCP server, or Python import; callable from Claude Code, Cursor, Codex CLI, or your own scripts. Quality numbers (synthetic-corpus, deterministic seed) and per-strategy benchmark in [`recall/README.md`](recall/README.md). PRs touching `recall/` gate on a 5pp recall@5 tolerance.

---

## Runtime: what's in the context window this turn

The runtime owns the **injection layer**: what is pushed through hooks, `CLAUDE.md`, and `UserPromptSubmit` re-injection. Manifest + token budgets + eviction policy + replay. Host-agnostic core (`runtime/core/`) plus a Claude Code adapter (`runtime/adapters/claude_code/`); planned Cursor + Codex CLI adapters slot into the same interface. Wire it once with `recall runtime install-hooks`, then it runs silently.

Plain English:

- **Manifest** is the runtime's table of contents: the files, tool results, and notes Brainstack believes are currently in the injected context.
- **Token budgets** are the space limits Brainstack sets for each bucket, such as `retrieved`, `hot`, and `scratchpad`.
- **Eviction policy** is the rule Brainstack uses when a bucket gets too full. The default is LRU: remove the least-recently-used unpinned item first.
- **Replay** rebuilds the session from the event log so you can see what was in context at each turn and what got dropped.

Example: Claude reads five Postgres notes at 500 tokens each. The `retrieved` bucket has a 2,000-token budget, so the fifth note pushes the bucket to 2,500 tokens. Brainstack records all five reads, runs the eviction policy, drops the oldest unpinned note from the manifest, and keeps the bucket at 2,000 tokens. Later, `recall runtime replay --diff 4:5` can show exactly which note was added and which note was evicted. This does not change Claude's private model memory; it controls and audits the context Brainstack injects.

### How we measure runtime quality

Runtime quality is measured as correctness under pressure, replay honesty, and hook overhead:

```bash
python3 -m pytest \
  tests/runtime/test_budget.py \
  tests/runtime/test_integration_live_replay.py \
  tests/runtime/test_performance.py -q
```

The budget gate proves the control property: once an item is evicted, it does **not** reappear unless a later explicit add event brings it back. The replay gate runs a live Engine session, writes an event log, replays it through a fresh Engine, and requires the final manifests to be **byte-identical**. The performance gate keeps the hot path bounded: hook p95 under **100 ms**, `Engine.apply` p95 under **5 ms**, replay of **500+ events under 1 second**, and token counting of about **900 KB under 1 second**.

For real-session quality, `tests/recall/bench_memory_quality.py` also reports eviction pressure from a local runtime log. On the current local log it found **449 events**, **168 Engine evictions**, and **72 same-source re-reads after eviction**. That is the runtime-quality number to improve: fewer regretful evictions, not just more total context.

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
