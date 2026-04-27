# Architecture

## The two-repo model

```
┌─────────────────────────────────────────────────────────────────┐
│ PUBLIC, SHAREABLE                                               │
│ github.com/mustafa-bharmal-shipveho/mustafa-agentic-stack       │
│   ├── install.sh, upgrade.sh                                    │
│   ├── agent/    ←── source of truth for tools + hooks + schemas │
│   ├── adapters/claude-code/                                     │
│   ├── templates/                                                │
│   ├── docs/                                                     │
│   ├── tests/                                                    │
│   ├── LICENSE  (Apache 2.0)                                     │
│   ├── NOTICE   (attribution to upstream agentic-stack)          │
│   └── UPSTREAM.md (vendored file inventory + pinned commit)     │
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

- **Vendoring at a pinned commit, not Git submodule** — upstream is on
  v0.11 and shipping fast. Vendoring lets us pin compatibility and
  rebase on our schedule, with `tests/test_schema_compat.py` to fail-loud
  on schema drift. See [`UPSTREAM.md`](../UPSTREAM.md) for the rebase process.

- **Private brain, public framework** — strict separation by repo. The
  installer never edits user settings; manual merge of the Claude Code
  hook snippet preserves any other hooks the user already has.

## What's missing at v0.1

- Multi-harness adapters (Cursor, Codex, Windsurf, etc.) — Claude Code only
- Data flywheel exporter (approved-runs export pipeline)
- Onboarding wizard (manual install only)
- Brew formula
- Windows installer (`install.ps1` is a placeholder)

See [`CHANGELOG.md`](../CHANGELOG.md) for the path forward.
