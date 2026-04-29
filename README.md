# brainstack

**A persistent, git-synced memory for your AI coding agent. One brain across every project, every session, every machine.**

LLMs keep getting smarter, but every new session starts cold. You re-explain context, repeat the same mistakes, and rediscover the same fix you found last week. This framework gives your agent a brain that survives the session — captures what happened, distills patterns into reusable lessons, and feeds them back into the next conversation automatically.

Built on top of [codejunkie99/agentic-stack](https://github.com/codejunkie99/agentic-stack), targeting a different design point: **one global brain at `~/.agent/`**, not per-project. Git-synced to a private repo so a laptop crash doesn't nuke years of accumulated lessons.

---

## The problem this solves

Modern AI coding agents (Claude Code, Cursor, Codex, Aider) are powerful but **stateless per session**. The agent's intelligence inside a conversation is bottlenecked not by the model — it's bottlenecked by how much it remembers from your prior conversations.

Concretely:
- You explain "we use pnpm not npm" three times a week.
- You hit the same Postgres migration footgun, and the agent helpfully suggests the same wrong fix it suggested last month.
- You discover an internal tool, document it in a project README, and three weeks later in a different repo the agent has no idea it exists.
- Your laptop dies and the entire `~/.claude/` directory goes with it.

The fundamental insight: **the model gets smarter every release, but your agent only gets smarter if its context does.** This framework is a context-management substrate, optimized for that.

---

## How it works

```
        ┌──────────────────────────────────────┐
        │ Claude Code (or Cursor / Codex / …)  │
        └──────────────┬───────────────────────┘
                       │ PostToolUse hook fires per tool call
                       ▼
            ~/.agent/memory/episodic/AGENT_LEARNINGS.jsonl
                       │  (sentinel-locked, atomic writes)
                       ▼
        Dream cycle (nightly via launchd)
        - clusters tool-call patterns by salience
        - promotes high-signal patterns to candidates/
                       │
                       ▼
        You review candidates: graduate.py / reject.py
                       │
                       ▼
            ~/.agent/memory/semantic/LESSONS.md
                       │
                       ▼
        Loaded into next session's CLAUDE.md context
        → agent knows what it learned yesterday
```

Loop closes daily. Each session writes new episodes; each night the dream cycle distills them; each morning the agent reads back the distilled lessons.

---

## Why not just use upstream agentic-stack?

The upstream project is excellent — we vendor 20 files from it (clustering, decay, lesson rendering) verbatim. But its design point is per-project, multi-harness:

| | upstream agentic-stack | this framework |
|---|---|---|
| Brain location | per-project `.agent/` | one global `~/.agent/` |
| Cross-project memory | no — fragmented per repo | yes — same brain everywhere |
| Multi-machine | not designed for | `git pull` on session start |
| Laptop-loss durability | brain on local disk only | mirrored to private git repo |
| Plug-in user repo | manual git setup | `./install.sh --brain-remote <url>` |
| Secret redaction | basic | 5-layer defense (see Security) |
| Atomic-write safety | basic | sentinel-locked; no inode-swap data loss |
| ReDoS protection | n/a | rejects pathological user regexes |
| BRAIN_ROOT hardening | n/a | validated under $HOME, hook-script presence required |
| Override audit | n/a | every `.agent-local-override` fire logged |
| Python compatibility | tied to its harness | 3.9 + 3.10 + 3.13 verified |
| Test coverage | TBD | 104 tests incl. fuzz + concurrency stress |
| Adapter set | 10 harnesses | Claude Code at v0.1 (others on roadmap) |

Per-project brains fragment context — you relearn the same lesson 10 times across 10 repos. Global persistence + git sync turns the same engine into a substrate that compounds over time.

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

Then manually merge the printed snippet into `~/.claude/settings.json` (the installer never edits user config — see [`docs/claude-code-setup.md`](docs/claude-code-setup.md)).

Migrate from an existing flat memory directory:

```bash
./install.sh --migrate ~/.claude/projects/<slug>/memory
```

Verify health any time:

```bash
./install.sh --verify
make report-status
```

---

## Retrieval (v0.2)

Once your brain has more than a few dozen lessons, the auto-loaded `MEMORY.md` index alone stops being enough — paraphrases miss, and the agent burns context reading whole files to find what it needs. The `recall` package (top-level `recall/`) is the read-side companion: a CLI + MCP server that does hybrid BM25 + embedding retrieval against the same `~/.agent/memory/` your write-side already populates.

Zero-config inside brainstack — `recall` reads `$BRAIN_ROOT` and indexes `$BRAIN_ROOT/memory/` automatically. You don't set up anything separately.

```bash
# Install retrieval extras (one-time)
pip install -e '.[embeddings,mcp]'

# Index your brain (read-only on the brain; cache lives at ~/.cache/recall/)
recall reindex

# Query
recall query "how do I avoid context bloat from reading too many files"
```

JSON output, top-K matches with curated `description` fields. Works from any shell — Claude Code, Cursor, Codex CLI, plain bash. For MCP-aware clients, register `recall-mcp`.

See [`recall/README.md`](recall/README.md) for the full retriever surface (filters, MCP setup, second-brain repos, retrieval-quality numbers).

<!-- recall-quality:start -->

### Retrieval quality (auto-updated by CI on every merge to main)

Last refresh: **2026-04-29** (scale 80 / 1,000 / 5,000 synthetic lessons).

The single number that matters: **how often does the retriever surface a
relevant lesson in the top 5 results, when the user asks a paraphrased
question** (a question that doesn't share words with the lesson title)?

| Brain size | Today<sup>1</sup> | Best case<sup>2</sup> | With `recall` (hybrid)<sup>3</sup> | Latency |
|---|---|---|---|---|
| **80 lessons (you today)** | 56% | 56% | **100%** | 10.2 ms |
| **1,000 lessons** | 12% | 38% | **100%** | 12.2 ms |
| **5,000 lessons** | 12% | 35% | **100%** | 38.0 ms |

<sup>1</sup> What you get if you only have `MEMORY.md` auto-loaded — the index
truncates at 200 lines, so past ~150 lessons most of your brain is invisible
to the LLM.
<sup>2</sup> Optimistic baseline: the LLM somehow has the *full* MEMORY.md in
context (e.g. you `Read` it explicitly). Even then, lexical matching tops out.
<sup>3</sup> Hybrid retrieval = BM25 keyword search + sentence-transformer
embeddings, fused with Reciprocal Rank Fusion. Indexes the full body of every
lesson, not just the description column.

**Latency** is per-query wall clock, warm-cache, on the CI runner. Add the
embedding-model load (~90 MB, one-time on first call) for cold start.

**Numbers come from `tests/recall/bench_e2e.py`** (synthetic corpus, fixed
seed — re-runs produce the same results). PRs that touch `recall/` are
gated on this metric: a PR fails CI if hybrid bucket-paraphrase recall@5
drops by more than 5 percentage points vs. the baseline checked in at
`tests/recall/bench_baseline.json`. See `tests/recall/BENCH_RESULTS.md`
for the full per-strategy breakdown.

<!-- recall-quality:end -->

---

## Architecture (v0.1)

```
~/.agent/
├── memory/
│   ├── working/         # ephemeral session state, REVIEW_QUEUE.md
│   ├── episodic/        # AGENT_LEARNINGS.jsonl (every tool call)
│   │   ├── AGENT_LEARNINGS.jsonl
│   │   └── AGENT_LEARNINGS.jsonl.lock  # sentinel; not the data file
│   ├── semantic/        # graduated lessons (lessons.jsonl + LESSONS.md)
│   ├── personal/        # profile, preferences, references, notes
│   ├── candidates/      # staged by dream cycle, awaiting your review
│   ├── _atomic.py       # temp+fsync+os.replace helpers
│   ├── auto_dream.py    # the nightly clustering pass
│   └── MEMORY.md        # human-readable index
├── tools/
│   ├── redact.py            # pre-commit secret scanner (16 vendor patterns + entropy)
│   ├── redact_jsonl.py      # sync-time JSONL scrubber (recursive, atomic)
│   ├── scrub_employer.py    # context-preserving identity scrubber
│   ├── dream_runner.py      # fcntl-based launchd entry point
│   ├── sync.sh              # hourly git push with required scanner
│   ├── graduate.py / reject.py / list_candidates.py / reopen.py
│   └── data_layer_export.py # dashboard exporter
├── harness/
│   └── hooks/
│       ├── agentic_post_tool_global.py  # BRAIN_ROOT-validated wrapper
│       ├── claude_code_post_tool.py     # the actual capture
│       └── _episodic_io.py              # sentinel-locked appender
├── redact-private.txt   # YOUR org-specific patterns (loaded by redact.py)
├── override.log         # audit trail of .agent-local-override fires
└── .git/                # pushed to your private GitHub remote
```

---

## Security posture

The brain holds tool-call history including raw Bash commands and Edit deltas. Pushing that to a remote without guardrails is a credential leak waiting to happen. The framework ships **5 layers of defense**, each documented in [`docs/redaction-policy.md`](docs/redaction-policy.md):

1. **Pre-commit `redact.py`** — 16 vendor token shapes (AWS, GitHub, OpenAI, Anthropic, Slack, Stripe, Sentry, Datadog, Google, Twilio, SendGrid, Heroku, NPM, Mailgun, JWT, Authorization headers, PEM blocks) + URL-aware Shannon entropy sweep.
2. **`redact-private.txt`** — your org-specific patterns, merged at scan time. ReDoS-prone regexes are rejected at load.
3. **Sync-time `redact_jsonl.py`** — recursive walk over every string field in episodic JSONL, replacing matches with `[REDACTED:<pattern>]`. Catches secrets that flow through tool calls before sync time.
4. **Required scanner at sync** — `sync.sh` refuses to push without `trufflehog` or `gitleaks` on PATH (`SYNC_ALLOW_NO_SCANNER=1` to override; not recommended).
5. **Server-side GitHub Action** — re-runs both scanners on every push/PR, catching `git commit --no-verify` bypasses.

Plus structural hardening:
- **BRAIN_ROOT validation** — wrapper rejects env-pointed brain paths outside `$HOME` or missing the vendored hook script (closes env-poisoning RCE).
- **Sentinel-locked atomic writes** — dream cycle locks `<jsonl>.lock`, not the data file itself, so `os.replace` doesn't invalidate concurrent appenders' locks. Verified by stress test: 0/800 lost rows under 20-way contention.
- **Override audit** — every `.agent-local-override` fire writes to `<brain>/override.log` so silently-disabled logging is detectable.
- **`scrub_employer.py`** — context-preserving identity scrubber for moving brains between accounts. Replaces specific identifiers with role-typed placeholders (e.g. `<your-employer>` → `Acme`, `<colleague-firstname>` → `Manager`) so the brain stays useful but loses direct attribution. Configurable map; ships with empty examples.

See [`docs/`](docs/) for full architecture, redaction policy, hook precedence, and threat model.

---

## What's in v0.1

- Vendored dream cycle from `codejunkie99/agentic-stack@v0.11.2` (20 files, 3,683 lines, pinned commit)
- Lessons.jsonl schema extension (`why` + `how_to_apply` fields, backward-compat) — see [`schemas/lessons.schema.json`](schemas/lessons.schema.json)
- Clean-room: `redact.py`, `redact_jsonl.py`, `scrub_employer.py`, `sync.sh`, `dream_runner.py`, `migrate.py`, the global hook wrapper
- Claude Code adapter (manual-merge snippet under [`adapters/claude-code/`](adapters/claude-code/))
- Data-layer dashboard exporter (vendored)
- 7 docs covering architecture, memory model, dream cycle, claude-code setup, git sync, redaction policy, hook precedence
- 104 tests (unit + fuzz + race-stress + e2e pipeline) on Python 3.9 and 3.13

---

## What's next

**v0.2 — Multi-harness:**
- Adapters for Cursor, Codex CLI, Windsurf, Aider — same brain, different IDE
- Onboarding wizard (`./install.sh --interactive`) for users who don't want to read docs
- Linux launch units (systemd) alongside the macOS launchd plists
- Brew tap for `brew install brainstack` (or its eventual rename)

**v0.3 — Smarter dream cycle:**
- Multi-machine append conflict resolution (timestamp-merge of episodic JSONL on `git pull`)
- Brain visualization dashboard ("what has my agent learned?") — built on the existing `data_layer_export.py`
- Better salience scoring — currently keyword-based; LLM-graded next

**v0.4 — Compounding intelligence:**
- Opt-in lesson sharing across users with shared org context (auto-redacted via `scrub_employer.py`)
- Cross-project lesson retrieval ("when working on repos like this, you learned…")
- Active-recall: agent occasionally re-uses old lessons unprompted to verify they still hold

**Always:** keep the security posture sharp. The framework's value is proportional to how much you trust putting your tool-call history into a remote — that trust comes from the redaction layers holding up under adversarial review.

---

## External consumers (v0.2-rc1)

External agent frameworks can read and write the brain through `agent/memory/sdk.py` using namespaces. The SDK exposes `append_episodic`, `query_semantic`, `read_policy`, `write_policy`, and `register_clusterer` — each takes a `namespace` arg matching `^[a-z][a-z0-9_-]{0,31}$`. Pluggable per-namespace dream-cycle clusterers live in `agent/dream/registry.py` (`run_all` aggregates results across namespaces). Backward compatibility is preserved: `namespace="default"` maps to the v0.1 paths (no extra subdir under `episodic/`, `semantic/`, `candidates/`), so existing v0.1 brains do not need migration. New CLI flags `--namespace NS` are now available on `graduate.py` / `reject.py`, and two new tools (`promote.py`, `rollback.py`) manage tier policy + audit log per namespace. See `mustafa-agents` (companion repo) for the reference TypeScript runtime that consumes this SDK.

## License

Apache 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE) for upstream attribution.

## Use at your own risk

This is personal infrastructure shared as-is. Issues and PRs welcome but no support obligations are implied.
