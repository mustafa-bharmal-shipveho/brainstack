# Claude Code adapter

The wiring needed to make Claude Code log every tool call into your global brain
at `~/.agent/`, with optional project-level overrides.

## Files

- **`settings.snippet.json`** — JSON snippet to merge into your existing
  `~/.claude/settings.json`. Adds one `PostToolUse` hook that calls the
  global wrapper.
- **`README.md`** — this file.

## Setup

1. **Install the brain** (creates `~/.agent/`):
   ```bash
   ./install.sh
   ```

2. **Merge the hook into your `~/.claude/settings.json`**. Open both files
   side-by-side and add the `PostToolUse` entry from the snippet to the
   array in your settings. Critical: do NOT replace the file — append to
   existing hooks (you may already have roux-cli, crystl, etc.).

   Example merged result:
   ```json
   {
     "hooks": {
       "PostToolUse": [
         { /* your existing hook A */ },
         { /* your existing hook B */ },
         {
           "matcher": "Bash|Edit|MultiEdit|Write|Task|TodoWrite",
           "hooks": [
             { "type": "command", "command": "python3 \"$HOME/.agent/harness/hooks/agentic_post_tool_global.py\"" }
           ]
         }
       ]
     }
   }
   ```

3. **Validate the JSON** before saving:
   ```bash
   python3 -m json.tool ~/.claude/settings.json > /dev/null
   ```

4. **Smoke-test** by running any Claude Code session and triggering a tool
   call (read a file, run a command). Then check:
   ```bash
   tail ~/.agent/memory/episodic/AGENT_LEARNINGS.jsonl
   ```
   You should see one new entry per tool call.

## Project-level override

If you clone a repo that already has its own upstream-`agentic-stack` `.agent/`
folder with its own hooks, you can prevent double-logging by creating an
override marker in the project root:

```bash
touch /path/to/project/.agent-local-override
```

When this file exists, the global wrapper exits 0 immediately for any tool
call inside that project's `$CLAUDE_PROJECT_DIR`, leaving the project's own
hooks to do the work.

## BRAIN_ROOT env override

For specialized work where you want a different brain location (e.g.,
isolation between work and personal):

```bash
export BRAIN_ROOT=~/work-brain  # in your shell profile or per-session
```

The hook will write to `$BRAIN_ROOT/memory/episodic/AGENT_LEARNINGS.jsonl`
instead of `~/.agent/`.

## What's NOT installed

The framework intentionally does NOT auto-edit your `~/.claude/settings.json`.
You merge by hand. This avoids accidentally overwriting existing hooks
(roux-cli, custom Notification handlers, etc.) and keeps you in control of
what's wired up.
