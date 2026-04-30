# brainstack

**A persistent, git-synced brain for your AI coding agent.**

One global memory at `~/.agent/`, surviving sessions, machines, and laptop crashes. Every tool call your agent makes becomes a memory. Patterns get distilled into lessons. Lessons compound across every project. Everything pushes to your private git remote on a timer — laptop loss doesn't cost you a single insight.

- **Graduation pipeline.** Every tool call → episodic log → nightly dream cycle clusters salient patterns → you review candidates → graduated lessons land in `semantic/` and are auto-loaded on every future session. Mistakes get codified once, never repeated.
- **Constant git sync.** Hourly `sync.sh` pushes the whole brain to your private remote (with required secret-scanner gate). Reinstall on a new machine and `git pull` brings back every lesson, every preference, every reference.
- **One brain, every project.** Global `~/.agent/` (not per-repo), so a lesson learned debugging Postgres in repo A is available the next time you touch Postgres in repo Z.

The model gets smarter every release. Your agent only gets smarter if its context does. This is the substrate for that.

Built on top of [codejunkie99/agentic-stack](https://github.com/codejunkie99/agentic-stack) — vendored dream cycle, clustering, lesson rendering. See [`UPSTREAM.md`](UPSTREAM.md).

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

Then merge the printed snippet into `~/.claude/settings.json` (the installer never edits user config — see [`docs/claude-code-setup.md`](docs/claude-code-setup.md)).

Migrating from a flat memory directory: `./install.sh --migrate ~/.claude/projects/<slug>/memory`. Verify health any time with `./install.sh --verify` or `make report-status`.

---

## How it works

```
              capture           distill            graduate           recall
Claude Code ─────────► episodic/ ────────► candidates/ ─────────► semantic/ ────────► next session
(PostToolUse hook)     JSONL log,           staged by              you review            auto-loaded via
                       sentinel-locked      dream cycle            graduate.py /         MEMORY.md → CLAUDE.md
                                            (nightly, launchd)     reject.py

                       ↻ git sync (hourly via launchd) — push to your private brain remote, scanner-gated
```

Five stages:

1. **Capture (every tool call).** `PostToolUse` hook writes to `~/.agent/memory/episodic/AGENT_LEARNINGS.jsonl`. Sentinel-locked so concurrent sessions don't corrupt the log.
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

## Roadmap

- **v0.2 — Multi-harness.** Adapters for Cursor, Codex CLI, Windsurf, Aider. Linux systemd units alongside macOS launchd. Brew tap. Interactive install wizard.
- **v0.3 — Smarter dream cycle.** Multi-machine append-conflict resolution. Brain visualization dashboard. LLM-graded salience (currently keyword-based).
- **v0.4 — Compounding intelligence.** Opt-in cross-user lesson sharing (auto-redacted). Cross-project retrieval ("when working on repos like this, you learned…"). Active-recall verification.

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
