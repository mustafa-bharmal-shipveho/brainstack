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
cp <your-brainstack-clone>/templates/pre-commit \
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
cp <your-brainstack-clone>/templates/com.user.agent-sync.plist \
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
3. **REQUIRED** `trufflehog` or `gitleaks` scan over the brain dir; any
   file the scanner flags is quarantined (unstaged, left dirty) rather
   than blocking the whole run — see "Quarantine and the size gate"
   below. (Set `SYNC_ALLOW_NO_SCANNER=1` to skip the scan entirely — not
   recommended; `install.sh` warns if neither scanner is on PATH.)
4. Stages all changes, then holds back any staged file over 50 MB (see
   below).
5. The pre-commit hook runs `redact.py` (with `redact-private.txt`
   patterns merged).
6. Commits with timestamp + pushes.
7. Writes `runtime/health.json` (via `recall health --write`) and
   refreshes `PENDING_REVIEW.md` — from a single `EXIT` trap, so this
   happens on every exit path, not just a successful push.

Logs land in `~/.agent/sync.log`.

## Quarantine and the size gate

Two independent per-file gates run before the commit, and both are
**fail-open per file** rather than fail-closed for the whole run — a
single bad file no longer blocks every other memory from syncing:

- **Secret quarantine.** Anything the scanner flags is unstaged (left
  dirty in the working tree) and logged as
  `sync: quarantined (not pushed): <path>`. The rest of the commit still
  pushes. The quarantined file re-checks itself on the next run and
  syncs once it's clean.
- **Size gate.** GitHub rejects any blob over 100 MB server-side; once
  that happens, the *entire* commit — every memory since the last
  successful push — sits unpushed until a human notices and untracks
  the offending file. To catch it earlier, `sync.sh` unstages any
  individually staged file bigger than `${SYNC_MAX_FILE_BYTES:-52428800}`
  bytes (50 MiB) — strictly greater-than, so a file already syncing at
  exactly the limit never starts stalling — and logs:
  ```
  sync: oversize (not pushed): <path> (<bytes> bytes > 52428800-byte limit)
  sync: held back <N> oversize file(s) (>50 MB); syncing the rest
  ```
  The oversize file stays dirty in the working tree, exactly like a
  quarantined one, so it re-syncs itself once rotation shrinks it (see
  `agent/harness/hooks/_episodic_io.py`'s size-triggered rotation) or a
  human untracks it. Override the threshold with `SYNC_MAX_FILE_BYTES`
  (bytes) if your remote's limit differs.

Both gates log under the `sync:` prefix; `render_pending_summary.py`'s
status classifier reads the *last* `sync:`-prefixed line in `sync.log`
to decide the run's overall status, so a partial (quarantined/oversize)
sync still shows as a successful push once the terminal `sync: pushed`
line lands. The `health:`-prefixed lines the EXIT trap appends after
that (see below) are deliberately a different prefix, precisely so they
never get mistaken for the run's terminal marker.

## Health check on every exit

`sync.sh` writes `~/.agent/runtime/health.json` from a single `EXIT`
trap, so a no-op run, a rejected commit, and a failed push all leave a
fresh report behind — the alternative (writing it only after a
successful push) would go quiet exactly when something needs attention.
It resolves the `recall` CLI the same way the rest of the fallback-dir
work in this slice does: `$RECALL_BIN` if set, else whatever `recall`
resolves to on `PATH`, else `~/.local/bin/recall`. If none of those
exist it logs `health: recall CLI not found; skipped` and moves on —
missing `recall` is never fatal to the sync itself. On a hit it runs:

```bash
recall health --json --brain-root "$BRAIN_ROOT" --cwd "$BRAIN_ROOT" \
    --write "$BRAIN_ROOT/runtime/health.json"
```

and logs `health: wrote runtime/health.json`. `PENDING_REVIEW.md` and
the Claude Code SessionStart banner both read that file.

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

`~/.agent/.gitignore` (rendered from `templates/brain.gitignore`)
excludes:
- `*.log` (regenerated)
- `.brain.lock`, `*.lock` (lockfiles)
- `__pycache__/`, `*.py[cod]` (Python bytecode)
- `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`
- `*.tmp` (atomic-write temp files; cleaned up by `sync.sh`)
- `data-layer/exports/` (derived dashboard outputs)
- `.index/` (FTS index, rebuildable)
- `.obsidian/` (per-machine vault config)
- `runtime/health.json`, `runtime/dream_status.json`,
  `runtime/recall.sock` — machine-local status files the sync and dream
  ticks regenerate every run; syncing them across machines would just
  cause churn.
- `runtime/logs/*.jsonl`, `runtime/logs/injected/`
- `memory/episodic/AGENT_LEARNINGS*.jsonl`,
  `memory/episodic/**/AGENT_LEARNINGS*.jsonl`,
  `memory/episodic/**/_imported.jsonl*`, `*.preCodexFix.bak` — the
  size-triggered rotation in `agent/harness/hooks/_episodic_io.py`,
  `agent/memory/_atomic.py`, and `runtime/core/locking.py` renames an
  oversize `AGENT_LEARNINGS.jsonl` / `events.log.jsonl` to a dated
  sibling instead of letting either grow past GitHub's 100 MB blob
  limit; the globs cover every rolled name so none of them need to sync.

`./install.sh --upgrade` (and a fresh install) appends any rule from the
template that's missing from your brain's live `.gitignore`, under a
`# brainstack --upgrade (vX): rules added` header. It only ever adds,
never removes: your own custom rules are always preserved, but note
that if you deliberately delete one of the *template's* rules, the next
`--upgrade` re-adds it (the check is presence-based, not a diff against
what you previously had). Running `--upgrade` twice in a row is a
byte-for-byte no-op the second time.

Source markdown, JSONL, and tools are committed.

## Recovery

A laptop crash recovers via:

```bash
# On the new machine:
git clone https://github.com/<your-account>/<your-private-repo>.git ~/.agent
cd <your-brainstack-clone>
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
