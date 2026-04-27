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

### `BRAIN_ROOT` is security-sensitive

The wrapper exec's a Python script under whatever `BRAIN_ROOT` resolves
to. Without validation, a hostile process or a sourced `.envrc` could
point `BRAIN_ROOT` at attacker-controlled Python (e.g.
`BRAIN_ROOT=/tmp/attacker/.agent`).

The wrapper enforces three constraints, and falls back to `~/.agent`
with a warning to `~/.agent/hook.log` if any of them fail:

1. The path must resolve (real-path, symlink-aware) under `$HOME`.
2. The path must contain `harness/hooks/claude_code_post_tool.py`
   (otherwise it's not a brain dir).
3. The path-traversal `..` is normalized away by `Path.resolve()`
   before the under-`$HOME` check.

If you need to override `BRAIN_ROOT`, point it at a directory you own
under your real home dir.

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

### Override fires are audited

Because a `.agent-local-override` marker silently disables global
logging for every tool call in that project, a malicious or careless
repo could ship the marker in its initial commit and you'd never notice
your tool usage stopped being captured.

Every override fire appends a line to `~/.agent/override.log`:

```
2026-04-27T03:14:15.926535+00:00\t/Users/me/projects/sketchy-repo\t/Users/me/projects/sketchy-repo/.agent-local-override
```

`tail ~/.agent/override.log` shows every project that has suppressed
logging recently. If a project shows up there that you didn't expect,
investigate the marker.

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
