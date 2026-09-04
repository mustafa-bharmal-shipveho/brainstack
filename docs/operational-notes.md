# Operational notes

This document keeps contributor/operator context that is too detailed for the
README but still matters when changing brainstack internals.

## Pending review visibility

The dream cycle writes candidate lessons to `~/.agent/memory/candidates/`.
Unreviewed candidates are intentionally not auto-graduated. Dogfood testing
showed that hidden pending work can pile up for days, so brainstack now renders
`~/.agent/PENDING_REVIEW.md` and exposes the count at session start.

Supported surfaces:

| Surface | Setup |
|---|---|
| Claude Code `@` import in `~/.claude/CLAUDE.md` | `./install.sh --setup-pending-hook` |
| Cursor rules | `./install.sh --setup-cursor-rules` |
| Shell wrappers for AI CLIs | `./install.sh --setup-shell-banner` |
| All of the above | `./install.sh --setup-pending-review-all` |

Dogfood note: a Claude Code `SessionStart` hook was tested as the first
implementation, but that hook path did not reliably inject context into fresh
sessions on the tested build. The `@` import path was chosen because it uses
Claude Code's normal session-load behavior.

## Health checks

`recall doctor` answers "is brainstack wired up?". `recall health` answers "is it
still working?". The distinction matters because every job that keeps a brain
current runs unattended: the hourly sync, the nightly dream cycle, the Claude
project mirror. When one of them stops, nothing tells you. On 2026-09-04 the
brain had been 67 commits behind for five days because a 107 MB episodic file
was being rejected by GitHub. The exact `remote: error:` line was in `sync.log`
the whole time, and `PENDING_REVIEW.md` said "the next hourly sync will retry",
so the wall looked like patience.

### The catalogue

`recall/health.py` holds nine checks. Every one is a pure function of an
injected `HealthEnv` (brain root, home, cwd, clock, platform, a command runner,
a socket connector), so the whole catalogue is unit-testable with no real git,
no `launchctl`, no network and no sockets. See `tests/recall/test_health.py`.

| id | Severity rules | Data source |
|---|---|---|
| `imports_freshness` | Lag between the newest `~/.claude/projects/<slug>/memory` file and the newest file under `<brain>/imports/claude/projects`. Over 2 h warns, over 24 h fails, no mirror at all fails. No source files skips. | Filesystem mtimes |
| `launch_agents` | Plist on disk but absent from `launchctl list` fails. Listed with a non-zero last exit warns. A missing `sync`/`dream` plist warns; a missing `claude-extras` plist warns only when there is something to mirror. `auto-migrate` and `recall-daemon` are optional and are never mentioned when absent. | `launchctl list` + `~/Library/LaunchAgents` |
| `brain_push` | Commits ahead of the upstream with a last push older than 3 h fails, and quotes the `remote: error:` line from the most recent `sync.log` run. No `.git` or no remote skips. | `git remote`, `rev-parse --abbrev-ref @{u}`, `rev-list --count`, `log -1 --format=%ct` |
| `large_tracked_files` | Any tracked file over 50 MB fails. Lists the three largest so the line stays readable in a banner. | `git ls-files -z` + `stat` |
| `log_sizes` | Any `runtime/logs/events.log*.jsonl` or `memory/episodic/**/AGENT_LEARNINGS*.jsonl` over 20 MB warns. Rolled files count. | Filesystem |
| `dream_cycle` | Prefers `runtime/dream_status.json`, falls back to `dream.log`'s mtime and last `dream cycle:` line. Older than 36 h fails; `llm_errors` containing `provider_unavailable` fails; other `llm_errors` warn. Neither file skips. | JSON / log text |
| `drift` | Loads `check_freshness.detect_drift` from the repo pinned in `<brain>/.brainstack-repo-path` (or the checkout `recall` was installed from) and warns with its summary. No repo skips. | `check_freshness` |
| `auto_recall_config` | Only runs when the global `runtime/pyproject.toml` sets `enable_auto_recall = true`. Fails when the config resolved from the current directory turns it back off, naming the shadowing file. | `tomllib` + `RuntimeConfig.load()` |
| `daemon` | Configured (plist or socket present) and the socket refuses a connection fails. Not configured skips. | Injected AF_UNIX connect |

Overall status is FAIL if any check fails, else WARN if any warns, else PASS.
`recall health` exits 1 only on FAIL: a WARN is information, and the hourly
LaunchAgent that runs this must not record a non-zero exit every hour for one.

### Where the report surfaces

The hourly sync writes `~/.agent/runtime/health.json` (schema version 1:
`generated_at`, `brain_root`, `cwd`, `status`, `counts`, `checks`). Writes go
through `write_report`, which is tmp-file plus `os.replace`, so a reader never
sees a half-written report. Three consumers read it:

- `recall health` / `recall doctor --health`, run by hand.
- The Claude Code `SessionStart` hook: one line per FAIL, evidence truncated to
  220 characters, then a pointer to `recall health`. It prints nothing when the
  report is clean or missing, and never returns non-zero.
- `~/.agent/PENDING_REVIEW.md`: a `## Health` section listing FAIL and WARN
  checks with their fixes, plus a headline marker. PASS and SKIP checks are left
  out; they belong in the full report, not in a banner read every session.

A report older than 26 h is treated as stale rather than trusted. A 40-hour-old
all-clear is not evidence that the brain is fine, it is evidence that the hourly
agent died. `load_report` returns `None` for a missing, corrupt or stale file;
pass `max_age_hours=0` to skip the age check and tell those cases apart.

All health lines that sync.sh writes to `sync.log` use a `health:` prefix, never
`sync:`. `render_pending_summary._check_sync_status` classifies a run from its
last `sync:` line, so a health line written by the exit trap after that marker
must not be able to shadow the run's verdict.

## Human-gated review

`recall pending --review` hands off to an interactive TTY-only triage flow. The
tool refuses to run without a TTY so an assistant cannot graduate or reject
candidates unattended. This is a structural rule, not just a prompt instruction.

When touching this area, keep these guarantees:

- The pending summary should tell the user to run `recall pending --review`.
- Review decisions must require explicit user input.
- Generated pending summaries are local operational state and should not be
  synced to the private brain remote.

## Framework purity

Brainstack should not ship real personal, employer, internal-service, or
customer-specific strings in framework code, docs, schemas, or tests. Use
generic examples such as `<your-org>`, `example-corp`, `internal-service`, and
`reviewer-agent`.

Exception: the canonical repository URL
(`github.com/mustafa-bharmal-shipveho/brainstack`) appears in install
instructions and the doc-truth test by deliberate decision. All examples,
fixtures, and schemas still use placeholders.

Before release, run a targeted string audit for known local/company terms and
confirm any remaining hits are legal provenance in `NOTICE` / `UPSTREAM.md`,
the canonical-URL carve-out above, or explicitly intentional documentation.

## Session digests

Raw tool-call logs are too noisy for long-term recall. Session digests summarize
Claude/Codex sessions into searchable markdown with title, domain tags,
decisions, learned context, and files touched. These digests are what recall
should surface when a user asks "did I work on this before?"

Related operators:

```bash
./install.sh --setup-digests
BRAIN_ROOT=$HOME/.agent python3 ~/.agent/tools/digest_cli.py backfill
BRAIN_ROOT=$HOME/.agent python3 ~/.agent/tools/digest_cli.py provider list
```

Digest-derived features include profile rollups, theme clustering, and proactive
context candidates. Keep prompts/framework code domain-agnostic; tags should be
extracted from session content, not from a fixed company taxonomy.

### Bounded incremental digests (per-hour budget)

A single session digest costs several minutes of real LLM time (`claude -p`,
haiku), so `sync_claude_extras.py`'s hourly LaunchAgent tick used to run
`digest_cli.py incremental` under the same 600s timeout as the near-instant
session/misc mirror adapters — on any hour with more than a couple of new
sessions the digest step could never finish in time and got killed every run,
so the LaunchAgent exited 1 forever even though the session and misc mirrors
had actually succeeded. `digest_cli.py incremental` now takes `--limit N`
(default 3) and `--max-seconds S` (default 1500): it processes at most N
pending (not-yet-digested) sessions and stops cleanly — no partial LLM call,
no kill — once S seconds have elapsed, printing a machine-readable
`digests: processed=P pending=Q elapsed_s=E budget_hit=<bool>` line. Progress
is sidecar-idempotent either way, so a bounded run never loses work, it just
leaves the rest for the next tick. `sync_claude_extras.py` gives the digest
step its own timeout (`BRAINSTACK_DIGEST_TIMEOUT_S`, default 1800s, separate
from the 600s adapter ceiling), forwards `BRAINSTACK_DIGEST_LIMIT` /
`BRAINSTACK_DIGEST_MAX_SECONDS` (defaults 3 / 1500) as `--limit`/
`--max-seconds`, logs the summary line, and writes
`runtime/digest_status.json` (`ts`, `processed`, `pending`, `elapsed_s`,
`budget_hit`) so a future health check can WARN when `pending` grows for
several ticks in a row. The run still exits 0 when the digest step completed
even with `pending > 0` — that's a backlog, not a failure — and exits 1 only
on a real failure (non-zero rc or an actual timeout kill).

## Auto-recall

Auto-recall is on by default in the full install since v0.6.0 (opt out with
`--no-auto-recall`; the `--minimal` install does not enable it) and is
currently implemented for Claude Code's `UserPromptSubmit` hook. It runs recall per user prompt, injects bounded results
for that turn, and records telemetry consumable by `recall stats`.

The operational tradeoff is latency versus context quality:

- Short prompts, slash commands, and bare acknowledgements should be skipped.
- Timeouts should fail open so chat is not blocked.
- Scores are retrieval similarity, not factual accuracy.
- Lower-score hits should be treated as context, not authority.

Other clients can use recall through CLI or MCP today. Per-prompt auto-injection
for another client should be implemented as a client-specific adapter rather
than by weakening the core recall contract.

## Runtime boundary

The runtime records and replays what brainstack injects. "Eviction" means an
item will not be re-injected by brainstack on later turns unless explicitly
added again. It does not mean brainstack can inspect or evict tokens from a
vendor model's private KV cache.

Keep this distinction explicit in docs and user-facing output.

## Provenance

Some memory-pipeline files are derived from `codejunkie99/agentic-stack` under
Apache 2.0. Keep attribution centralized in `NOTICE` and `UPSTREAM.md`, and do
not remove those files or their file lists when refactoring the README.

`recall stats --utilization` writes its LLM-judge sample (raw prompt and response text) to `$XDG_CACHE_HOME/recall/utilization_sample.json` by default, never under the brain root; pass `--sample-out` to choose another location outside the brain.
