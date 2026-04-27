# Git sync

`~/.agent/` is a git repo. The sync script (`tools/sync.sh`) commits
incremental changes hourly via launchd and pushes them to a private
GitHub repo. The brain survives a laptop loss without you having to
think about it.

## One-time setup

Inside `~/.agent/`:

```bash
cd ~/.agent
git init
git branch -m main
git remote add origin git@github.com:<your-account>/<your-private-repo>.git
git add .
git commit -m "Initial brain"
git push -u origin main
```

Use a **private** repo. The brain contains personal memory entries
that should never be public.

## Pre-commit hook

Install the pre-commit hook to block accidental secret commits:

```bash
cp ~/Documents/codebase/mustafa-agentic-stack/templates/pre-commit \
    ~/.agent/.git/hooks/pre-commit
chmod +x ~/.agent/.git/hooks/pre-commit
```

The hook runs `tools/redact.py` over every commit. It catches:
- AWS access keys (`AKIA[16chars]`)
- GitHub PATs / OAuth / server / refresh tokens
- JWT-shaped tokens
- Generic `api_key=` / `secret=` / `password=` / `token=` patterns
  with 30+ char values

False positives are suppressed by per-line marker:

```yaml
# redact-allow: example value used in test fixture
EXAMPLE_KEY = "AKIAIOSFODNN7EXAMPLE"
```

## Automated hourly sync

```bash
cp ~/Documents/codebase/mustafa-agentic-stack/templates/com.user.agent-sync.plist \
    ~/Library/LaunchAgents/

# Edit REPLACE_HOME placeholder, then:
launchctl load ~/Library/LaunchAgents/com.user.agent-sync.plist
```

Every 60 minutes:
1. Acquires `flock` on `~/.agent/.brain.lock` (waits if dream cycle is running)
2. Stages all changes
3. (If `trufflehog` is installed) runs entropy scan; aborts on any finding
4. The pre-commit hook runs `redact.py`
5. Commits with timestamp + pushes

Logs land in `~/.agent/sync.log`.

## Veho-aware private redaction

The framework's `redact.py` covers public token formats. For
org-specific patterns (Veho API keys, internal hostnames, etc.), edit
`~/.agent/redact-private.txt` (created by the installer):

```
# Private redaction patterns
# One regex per line (Python syntax)
# Example:
# (?i)veho[_-]?api[_-]?key\s*[:=]\s*[A-Za-z0-9_-]{20,}
```

This file is local to your brain (and gets committed to your private
repo, but never to the public framework).

## What gets gitignored

`~/.agent/.gitignore` excludes:
- `*.log` (regenerated)
- `.brain.lock` (lockfile)
- `__pycache__/`, `*.pyc` (Python bytecode)
- `.pytest_cache/`
- `data-layer/exports/` (derived dashboard outputs)
- `.index/` (FTS index, rebuildable)

Source markdown, JSONL, and tools are committed.

## Recovery

A laptop crash recovers via:

```bash
# On the new machine:
git clone https://github.com/<your-account>/<your-private-repo>.git ~/.agent
cd ~/Documents/codebase/mustafa-agentic-stack
./install.sh --upgrade   # refresh tools/hooks (memory/ untouched)
```

Then re-merge the Claude Code hook snippet into `~/.claude/settings.json`
(the framework doesn't auto-install settings on new machines).

## Multi-machine

For two laptops sharing the same brain:

- One laptop is the "primary" — it pushes hourly
- The other does `git pull` on session start (a SessionStart hook in
  `~/.claude/settings.json` works well for this)

Conflicts are unlikely if only one machine writes per session, but if
they happen, the canonical resolution is:
- `lessons.jsonl` is append-only — merge by sorting by `id`
- `MEMORY.md` — re-run `migrate.py` to regenerate from the truth files
