# Hook precedence

Three modes for how the global Claude Code hook (`hooks/agentic_post_tool_global.py`)
decides where to write episodic events.

## Mode 1: default (write to `~/.agent/`)

If neither `BRAIN_ROOT` env var is set nor a `.agent-local-override`
file exists in `$CLAUDE_PROJECT_DIR`, the hook writes to:

```
~/.agent/memory/episodic/AGENT_LEARNINGS.jsonl
```

Use case: most of your Claude Code work goes into the global brain.

## Mode 2: `BRAIN_ROOT` env override

If `BRAIN_ROOT` is set in the shell environment (or in the launch
context), the hook writes to:

```
$BRAIN_ROOT/memory/episodic/AGENT_LEARNINGS.jsonl
```

Use case: you have multiple brains for different contexts (e.g., a
work brain at `~/.work-brain/` and a personal brain at `~/.agent/`),
and you set `BRAIN_ROOT` per shell session or per project.

To set per project, use `direnv` or shell startup files. Example
`.envrc`:
```bash
export BRAIN_ROOT=~/.work-brain
```

## Mode 3: `.agent-local-override` file (skip the global hook)

If `$CLAUDE_PROJECT_DIR/.agent-local-override` exists (any contents,
including empty), the global wrapper exits 0 immediately without
writing anywhere.

Use case: you cloned a repo that already has its own
upstream-`agentic-stack` `.agent/` folder with project-local hooks.
You don't want the global hook duplicating work the project's own
hooks do.

To opt a project out:
```bash
cd /path/to/project
touch .agent-local-override
git add .agent-local-override   # commit it so teammates inherit
```

## Precedence order

1. `.agent-local-override` exists in `$CLAUDE_PROJECT_DIR` → exit 0, no write
2. Else if `BRAIN_ROOT` env var is set and non-empty → write to that path
3. Else → write to `~/.agent/`

Tested in `tests/test_hook_precedence.py`.

## Why these three modes

The global brain at `~/.agent/` is convenient for most users (one
accumulated brain across every project). But:

- Some users want **isolated brains per context** (work vs. personal
  vs. open-source contributions). Mode 2 supports this.
- Some users **clone repos that already use upstream agentic-stack**.
  Forcing the global brain to also fire would either cause double-logging
  or fight the project's own hook design. Mode 3 cleanly opts out.

## What the global hook does NOT do

- It does **not** read `~/.claude/settings.json` to discover other
  hooks. Whatever is wired up in your settings runs independently.
- It does **not** dispatch to project-level hooks. If you want both,
  add a separate `PostToolUse` entry in `settings.json` for the
  project hook (with a `matcher` that targets that specific project's
  files).
- It does **not** propagate failures back to Claude Code. A hook
  failure (e.g., `~/.agent/` doesn't exist yet) is silently logged to
  stderr and the wrapper exits 0. Tool flow keeps running.
