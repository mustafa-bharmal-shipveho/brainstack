# Architecture

## The two-repo model

```
┌─────────────────────────────────────────────────────────────────┐
│ PUBLIC, SHAREABLE                                               │
│ github.com/mustafa-bharmal-shipveho/brainstack                  │
│   ├── install.sh, upgrade.sh                                    │
│   ├── agent/    ←── source of truth for tools + hooks + schemas │
│   ├── adapters/claude-code/                                     │
│   ├── templates/                                                │
│   ├── docs/                                                     │
│   ├── tests/                                                    │
│   ├── LICENSE  (Apache 2.0)                                     │
│   ├── NOTICE                                                    │
│   └── UPSTREAM.md                                               │
│                                                                 │
│ Anyone can fork, install, run. No personal data lives here.     │
└─────────────────────────────────────────────────────────────────┘
                                │
                                │ install.sh copies agent/ → ~/.agent/
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│ PRIVATE, USER-OWNED                                             │
│ ~/.agent/  ←  pushed to a private GitHub repo                   │
│   ├── memory/                                                   │
│   │   ├── working/      ephemeral session state                 │
│   │   ├── episodic/     tool-call history (AGENT_LEARNINGS.jsonl)│
│   │   ├── semantic/     graduated lessons + LESSONS.md          │
│   │   ├── personal/     profile, preferences, references, notes │
│   │   └── candidates/   staged-by-dream, awaiting review        │
│   ├── tools/            (synced from public repo on upgrade)    │
│   ├── harness/hooks/    (synced from public repo on upgrade)    │
│   ├── redact-private.txt   user-owned org-specific patterns     │
│   └── .git/             pushed hourly to private remote         │
│                                                                 │
│ Personal data lives here. Never published.                      │
└─────────────────────────────────────────────────────────────────┘
                                │
                                │ Claude Code's PostToolUse hook
                                │ (configured in ~/.claude/settings.json)
                                ▼
                Every tool call appends to AGENT_LEARNINGS.jsonl
```

## Lifecycle

1. **Tool call** — Claude Code invokes the global hook
   (`hooks/agentic_post_tool_global.py`) which appends one JSONL row to
   `~/.agent/memory/episodic/AGENT_LEARNINGS.jsonl` per matched tool use
   (Bash, Edit, MultiEdit, Write, Task, TodoWrite).

2. **Nightly dream cycle** — launchd fires `auto_dream.py` at 03:00. It:
   - Holds an exclusive flock on `~/.agent/.brain.lock`
   - Loads `AGENT_LEARNINGS.jsonl`
   - Clusters recurring patterns (no LLM, mechanical)
   - Stages candidates in `~/.agent/memory/candidates/`
   - Runs heuristic prefilter (length + exact-duplicate)
   - Decays old episodic entries
   - Writes `working/REVIEW_QUEUE.md` summary

3. **Manual review** — invoking `/dream` in a Claude Code session walks
   the review queue and decides graduate-or-reject for each candidate
   with **required rationale**. Decisions are logged.

4. **Graduate** — `tools/graduate.py` appends to
   `semantic/lessons.jsonl` (one row per accepted lesson with optional
   `why` / `how_to_apply` / `original_markdown_path` extension fields)
   and re-renders `LESSONS.md`.

5. **Hourly sync** — launchd fires `tools/sync.sh`. It:
   - Holds the same flock as dream (no torn writes)
   - Stages all changes
   - Runs trufflehog if installed
   - Pre-commit hook (`redact.py`) blocks any matched secrets
   - Commits with timestamp + pushes to private remote

## Why these choices

- **Global brain at `~/.agent/`** — one accumulated brain across every
  project the user works in. Lessons learned in repo A apply to repo B.

- **Mechanical staging + manual review** — the dream cycle does no LLM
  reasoning, so it's safe to cron. The model thinks during review (the
  high-stakes step), not during consolidation (the boring step).

- **Required rationale on graduate AND reject** — preserves decision
  history. Recurring churn is visible (a candidate rejected twice and
  then accepted has all three reasons on record).

- **Private brain, public framework** — strict separation by repo. The full
  install registers Claude Code hooks in `~/.claude/settings.json` after
  printing its plan and asking for consent (opt out with `--no-auto-recall`);
  the `--minimal` install edits no host settings at all. Existing hooks are
  always preserved, never replaced.

## What's missing

- Per-prompt injection adapters for Codex CLI and Cursor (today those hosts
  get recall-first directives plus `recall-mcp`; Claude Code gets
  every-prompt injection)
- Published benchmarks (LongMemEval, auto-recall on/off A/B)
- PyPI / MCP-registry distribution so `uvx` works without a clone
- Native Windows installer (`install.ps1` is a placeholder; WSL2 works)

See [`ROADMAP.md`](../ROADMAP.md) for direction and
[`CHANGELOG.md`](../CHANGELOG.md) for history.
