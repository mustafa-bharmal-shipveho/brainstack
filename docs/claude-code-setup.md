# Claude Code setup

Step-by-step wiring of the brain into your Claude Code config.

## 1. Install the brain

```bash
git clone https://github.com/mustafa-bharmal-shipveho/mustafa-agentic-stack.git
cd mustafa-agentic-stack
./install.sh
```

This creates `~/.agent/` with the 4-layer memory scaffolding, all tools,
and the hook wrapper. It does **not** edit `~/.claude/settings.json`.

## 2. (Optional) Migrate an existing flat memory directory

If you've been using Claude Code's auto-memory at
`~/.claude/projects/<slug>/memory/` and want to bring those entries
into the new layered structure:

```bash
./install.sh --migrate ~/.claude/projects/<slug>/memory
```

The migration parses `feedback_*.md`, `user_*.md`, `project_*.md`,
`cycle-*.md`, `reference_*.md` patterns and routes each into the
correct layer.

After migration, **symlink** the old auto-memory location to point at
the new brain so Claude Code's auto-memory feature loads from the same
place:

```bash
mv ~/.claude/projects/<slug>/memory ~/.claude/projects/<slug>/memory.flat-pre-install
ln -s ~/.agent/memory ~/.claude/projects/<slug>/memory
```

(The flat backup preserves your originals; delete it after a couple of
sessions when you're confident the migration is good.)

## 3. Wire up the PostToolUse hook

Open `~/.claude/settings.json` in your editor. Find the `hooks.PostToolUse`
array (create it if missing). Add this entry **alongside any existing
hooks** — do **not** replace them:

```json
{
  "hooks": {
    "PostToolUse": [
      // ...existing hooks (roux, crystl, custom...) — leave them...
      {
        "matcher": "Bash|Edit|MultiEdit|Write|Task|TodoWrite",
        "hooks": [
          {
            "type": "command",
            "command": "/opt/homebrew/bin/python3.13 /Users/yourname/.agent/harness/hooks/agentic_post_tool_global.py"
          }
        ]
      }
    ]
  }
}
```

**Use absolute paths — do NOT use `$HOME` or `~` in the command.** The
hook command is shell-evaluated by Claude Code; a hostile `$HOME`
environment variable (set by a sourced `.envrc` in some project) could
otherwise redirect the hook to attacker-controlled Python. The
installer prints the resolved absolute paths it recommends — paste
those literally.

Replace `/opt/homebrew/bin/python3.13` with whatever Python 3.10+
interpreter you have. If you don't know:

```bash
which python3.13 python3.12 python3.11 python3.10
```

Validate the JSON before saving:

```bash
python3 -m json.tool ~/.claude/settings.json > /dev/null
```

## 4. Smoke test

Open any Claude Code session. Run a tool call (read a file, run a Bash
command). Then check:

```bash
tail ~/.agent/memory/episodic/AGENT_LEARNINGS.jsonl
```

You should see one new JSONL row per matched tool call. If the file
stays empty, the hook isn't firing — review the matcher pattern and
the command path.

## 5. Repurpose `/dream`

If you have an existing `~/.claude/commands/dream.md`, replace it with
the review-staged-candidates flow:

```bash
cp ~/Documents/codebase/mustafa-agentic-stack/templates/dream-command.md.template \
    ~/.claude/commands/dream.md
```

Now `/dream` walks `tools/list_candidates.py` output and prompts you to
graduate or reject each with required rationale.

## 6. Set up nightly dream + hourly sync

See [`git-sync.md`](git-sync.md).

## What if I don't want the global hook for some projects?

Drop a `.agent-local-override` file in any project root:

```bash
cd /path/to/project
touch .agent-local-override
```

The global hook will skip that project entirely. See
[`hook-precedence.md`](hook-precedence.md).

## Coexistence with roux, crystl, and other hooks

The framework's snippet adds **one** new entry to the `PostToolUse`
array. It does not touch any other hook category (Notification,
PreToolUse, Stop, etc.) and does not modify existing PostToolUse
entries. Other hooks fire independently:

```json
"PostToolUse": [
  { "matcher": "*", "hooks": [{ "command": "bash ~/.claude/crystl-hook.sh PostToolUse", ... }] },
  { "matcher": "Bash|Edit|...", "hooks": [{ "command": "python3 ... agentic_post_tool_global.py", ... }] }
]
```

Both run on every matching tool call.

## Troubleshooting

**Symptom: `AGENT_LEARNINGS.jsonl` stays empty after tool calls**

Check Claude Code's hook log (varies by version; usually visible in
verbose mode). Common causes:
- Wrong Python path in the hook command
- `~/.agent/` doesn't exist (re-run `./install.sh`)
- `.agent-local-override` exists in your project root unintentionally

**Symptom: `python3 ... agentic_post_tool_global.py` errors with**
`unsupported operand type(s) for |: 'type' and 'NoneType'`

You're using Python <3.10. The vendored hook code uses 3.10+ syntax.
Use `python3.13` (or any 3.10+) in the command.

**Symptom: dream cycle never runs**

Check launchd:
```bash
launchctl list | grep agent-dream
launchctl print user/$(id -u)/com.user.agent-dream
```

If the job isn't loaded, `launchctl load ~/Library/LaunchAgents/com.user.agent-dream.plist`.
