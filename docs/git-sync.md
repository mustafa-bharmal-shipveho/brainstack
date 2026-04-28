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
cp ~/Documents/codebase/brainstack/templates/pre-commit \
    ~/.agent/.git/hooks/pre-commit
chmod +x ~/.agent/.git/hooks/pre-commit
```

The hook runs `tools/redact.py` over every commit. It catches the full
pattern set documented in [docs/redaction-policy.md](redaction-policy.md):
AWS / GitHub / OpenAI / Anthropic / Slack / Stripe / Sentry / Datadog /
Google API keys, JWTs, Authorization headers, PEM private key blocks, and
generic high-entropy strings (with URL-aware exemption).

It also loads `~/.agent/redact-private.txt` and merges in any user-supplied
patterns there.

False positives are suppressed by per-line marker:

```yaml
# redact-allow: example value used in test fixture
EXAMPLE_KEY = "AKIAIOSFODNN7EXAMPLE"
```

The `git commit --no-verify` flag bypasses the local hook entirely. To
catch that, install the GitHub Action workflow at
`templates/brain-secret-scan.yml` into the brain repo's
`.github/workflows/secret-scan.yml`.

## Automated hourly sync

```bash
cp ~/Documents/codebase/brainstack/templates/com.user.agent-sync.plist \
    ~/Library/LaunchAgents/

# Edit REPLACE_HOME placeholder, then:
launchctl load ~/Library/LaunchAgents/com.user.agent-sync.plist
```

Every 60 minutes:
1. Acquires the brain-wide lock via `flock(1)` if available, else via a
   Python `fcntl.flock` fallback. Backs off (exit 0) if the dream cycle
   is mid-run.
2. Runs `tools/redact_jsonl.py` to scrub secrets that the post-tool hook
   captured into episodic JSONL before redaction had a chance.
3. **REQUIRED** `trufflehog` or `gitleaks` scan over the brain dir;
   aborts on any finding. (Set `SYNC_ALLOW_NO_SCANNER=1` to skip — not
   recommended; `install.sh` warns if neither is on PATH.)
4. Stages all changes.
5. The pre-commit hook runs `redact.py` (with `redact-private.txt`
   patterns merged).
6. Commits with timestamp + pushes.

Logs land in `~/.agent/sync.log`.

## Org-aware private redaction

The framework's `redact.py` covers public token formats. For
org-specific patterns (your employer's API keys, internal hostnames,
etc.), edit `~/.agent/redact-private.txt` (created by the installer):

```
# Private redaction patterns
# One regex per line (Python syntax)
# Example (replace `acme` with your org slug):
# (?i)acme[_-]?api[_-]?key\s*[:=]\s*[A-Za-z0-9_-]{20,}
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
cd ~/Documents/codebase/brainstack
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
