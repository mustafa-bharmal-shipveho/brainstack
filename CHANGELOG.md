# Changelog

## Unreleased — v0.2.0 context runtime (2026-05-01)

The runtime layer. brainstack is now storage + retrieval + runtime — the
only memory project shipping all three layers as one tool-agnostic stack.

### Added

- **`runtime/` module** — host-agnostic core (`runtime/core/`) plus
  Claude Code adapter (`runtime/adapters/claude_code/`).
- **Manifest schema v1.1** (`runtime/core/manifest.py`). Deterministic
  byte-identical round-trip. Per-item `score` field for replay-able
  recency-weighted policy. Per-item `x_*` extension preservation.
  Schema version validation on load with explicit non-`x_` rejection.
- **Event log schema v1.1** (`runtime/core/events.py`). Append-only
  JSONL of typed hook events. Each PostToolUse carries full
  `InjectionItemSnapshot` records so replay reconstructs manifests
  from events alone. `event_id` derived from natural key.
  `tool_input_keys` sorted at dump (PYTHONHASHSEED-safe).
- **Pluggable token counter** (`runtime/core/tokens.py`). Deterministic
  offline default (~±15% of vendor counts on English prose); zero
  external dependencies. Optional Anthropic-validating slot for users
  who want exactness.
- **Eviction policies** (`runtime/core/policy/defaults/`). LRU,
  recency-weighted, pinned-first. Pure functions: events in,
  evictions out. No I/O, no clock reads, no randomness.
- **Engine state machine** (`runtime/core/budget.py`). Receives typed
  events, maintains current injection set, enforces token budgets per
  bucket, calls `Policy.choose_evictions` when over cap. Pinned items
  never evicted. The control-property test pins the contract: an
  evicted item does not reappear without an explicit `AddItem`.
- **Replay engine** (`runtime/core/replay.py`). Reads `events.log.jsonl`,
  plays through Engine, emits per-turn manifests. Diff renderer for
  "what entered and left between turn N and turn N+1". The headline
  audit feature: integration test proves byte-identical manifests
  between live run and replay.
- **Atomic locking primitives** (`runtime/core/locking.py`).
  `locked_append` and `locked_write` using fcntl flock on a sentinel
  file (sibling `.name.lock`, never the data file — preserves the
  brainstack `_atomic.py` lesson).
- **Claude Code adapter** (`runtime/adapters/claude_code/`).
  `handle_hook(event)` entrypoint, `RuntimeConfig` loaded from
  pyproject.toml `[tool.recall.runtime]`, idempotent installer for
  `~/.claude/settings.json`.
- **`recall runtime` CLI subcommand group**: `ls`, `pin`, `unpin`,
  `evict`, `replay [--diff TURN_A:TURN_B]`, `budget`, `install-hooks
  [--dry-run]`. Wired into the existing `recall` typer app.
- **228 new tests** under `tests/runtime/` covering schemas, engine
  state, replay, adapter hooks, CLI, performance micro-benchmarks
  (p95 thresholds for handle_hook, Engine.apply, locked_append, replay
  of 500-event log), data leak battery (8 fake-secret patterns x 3
  surfaces), policy determinism stress (4 seeds x 3 policies x 5 runs
  byte-identical), timestamp/locale independence, path normalization.
- **Hook telemetry harness** (`runtime/_empirical/harness/`,
  research-only, gitignored data dir) for measuring hook deliverability
  under real Claude Code sessions.
- **`docs/runtime.md`** — full design + schema reference + roadmap.

### Security

- `OutputSummary.sha256` is **empty by default**. Callers opt in via
  `summarize_output(text, include_hash=True)`. A stable hash of
  secret-bearing output is a fingerprint risk against breach DBs.
  v0.x will add HMAC-keyed alternative.
- Manifest and event extensions bounded to **1 KiB** per `x_*` value
  at dump time. Prevents adapters from smuggling raw payload via the
  extension mechanism.
- Reference-only by default. Manifests + event log carry path + sha256
  + token count, never raw content. Opt-in `capture_raw=true` writes
  to a separate `~/.agent/runtime/logs/raw-payloads.jsonl` that is
  git-ignored.

### Reviews

- Codex code review APPROVE (7 of 7 checks).
- Skeptic + Security personas BLOCK on Phase 1 → conceptual issues
  resolved by Phase 3 (Engine adds control), structural issues fixed
  in code (per-item `score`, `x_*` preservation, sha256 default-off,
  extension max-size). Documented in `runtime/_review_outputs/SUMMARY.md`.

### Honest scope

The runtime owns the *injection layer* — what we push through hooks,
CLAUDE.md, and re-injection. It does NOT own the model's KV cache or
accumulated conversation history. "Eviction" means
"demotion-from-injection on the next turn." This boundary is in the
README and `docs/runtime.md` first 200 words.

---

## Unreleased — Multi-tool migration series (2026-04-30)

Five PRs (#6 → #10) shipped together, turning brainstack from "Claude Code
only" into a multi-tool brain that ingests Claude Code, Cursor plans, and
Codex CLI sessions automatically.

### Added

- **Multi-tool adapter chassis** (PR #7) at `agent/tools/migrate_dispatcher.py`.
  Pluggable `Adapter` Protocol with public `register_adapter` / `unregister_adapter` /
  `get_adapter_for` / `discover_candidates`. `MigrationResult` is JSON-serializable
  with `schema_version` + `tool_specific` escape hatch.
- **Cursor adapter** (PR #8) at `agent/tools/cursor_adapter.py`. Ingests
  `~/.cursor/plans/*.plan.md` byte-for-byte into `personal/notes/cursor/`
  under namespace `cursor`.
- **Codex CLI adapter** (PR #9) at `agent/tools/codex_adapter.py`. Ingests
  `~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-*.jsonl` + `~/.codex/history.jsonl`
  into `episodic/codex/AGENT_LEARNINGS.jsonl`. Offset-tracked idempotency:
  re-runs only import the byte range appended since the last run, so
  hourly ticks against a 7,000-episode codex history complete sub-second.
- **Auto-migrate setup wizard** (PR #10): `./install.sh --setup-auto-migrate`
  installs ONE LaunchAgent (`com.brainstack.auto-migrate`) that runs every
  enabled tool sequentially under a global fcntl lock. After running the
  wizard, no manual intervention needed — Cursor + Codex flow into the
  brain hourly. Non-interactive flags `--enable`, `--disable`, `--all`,
  `--none`, `--dry-run`, `--print-plist` for CI / dotfile bootstrap.
  Tear-down via `--remove-auto-migrate`.
- **Discovery flow** (PR #7): `./install.sh --migrate` with no source path
  drops into an interactive wizard that auto-detects what's on disk
  (Claude Code project memories, Cursor plans, Codex CLI sessions) and
  lets you pick what to import. Plus `--dry-run` for preview.

### Changed

- `install.sh --migrate` is no longer Claude-only. The non-dry path
  detects the source format via the dispatcher and routes to the right
  adapter. Cursor + Codex sources are ingested as snapshots (the
  `--symlink-native` swap is suppressed — those tools keep writing to
  their own dirs; only Claude Code's flat / nested memory gets the
  symlink).
- Manual `dispatch()` calls now hold the same fcntl lock the auto-migrate
  LaunchAgent does, preventing races on the shared `_imported.jsonl`
  sidecar.

### Hardening (codex review across 4 passes)

- Plist generated via Python `plistlib`, not `sed` — paths with spaces /
  XML metacharacters round-trip correctly.
- Modern `launchctl bootout`/`bootstrap` API (refused under sudo).
- Brain root resolved to absolute before plist generation (relative
  paths produced unusable LaunchAgents).
- `--dry-run` honored at function level — no filesystem writes during
  preview.
- 23 tests for the auto-migrate path with mocked `launchctl`. Total
  test count: 269 → 292.

## Unreleased — Lossless native-dir migration (2026-04-29)

Closes 4 documented losslessness gaps in `agent/tools/migrate.py` + `install.sh`
so that migrating a Claude Code / Cursor native auto-memory directory into the
brainstack `~/.agent/memory/` format never drops content.

### Migration

- **Recursive walk preserves nested target-shaped paths.** `personal/profile/`,
  `personal/notes/`, `personal/references/`, `semantic/lessons/` (and deeper
  subdirs like `semantic/lessons/sub-archive/`) round-trip verbatim. Was:
  shallow `iterdir()` silently dropped nested files.
- **`MEMORY.md` hook annotations survive index regeneration.** `parse_index_hooks()`
  reads source MEMORY.md and re-applies `— hook text` suffixes onto matching
  entries in the new index. Was: hooks discarded.
- **Frontmatter fields carried into `lessons.jsonl`.** `name`, `type`, and
  `originSessionId` (mapped to snake_case `source_session_id` to avoid
  collision with the v0.3 episode `origin` discriminator) are now structured
  columns on lesson rows. Was: only `description` consulted.
- **`install.sh --migrate` symlinks native dir → brain by default.** New
  `--symlink-native` (default) / `--no-symlink` flags. After migration, the
  source dir is moved to `<source>.bak.<unix-ts>.<random>` and replaced with
  a symlink to `$BRAIN_ROOT/memory`. Atomic-ish swap: temp symlink created
  first, source moved to backup, temp renamed into place — failures at any
  step preserve the original data and surface a recovery message.

### Hardening (from persona review)

- **Atomic writes in `migrate.py`.** Companion `.md`, `lessons.jsonl`, and
  regenerated `MEMORY.md` now go through `_atomic.atomic_write_*`. SIGKILL
  during migration leaves the previous file intact (matches the v0.1.1
  hardening of `auto_dream` / `promote` / `review_state`).
- **Self-recursion guard.** `migrate.py` refuses to run when source and target
  overlap, and skips symlinked files during the recursive walk. Closes a
  symlink-as-file exfiltration vector and prevents direct re-invocation on
  a post-install symlink from walking the brain itself.
- **Pre-existing user-owned symlinks respected.** If `<source>` is already
  a symlink to somewhere other than the brain (advanced setups, dotfiles
  repos, network mounts), `install.sh --migrate` refuses rather than
  silently overwriting it.
- **Portable readlink resolution.** Idempotency check uses Python
  `os.path.realpath` (already required ≥ 3.10) instead of a `cd; pwd -P`
  dance that broke on relative symlink targets and missing brain dirs.
- **Mutually exclusive flag conflict refused.** Passing both `--symlink-native`
  and `--no-symlink` errors with exit 2 rather than silently using whichever
  appeared last.

### Tests

- 5 → 20 in `tests/test_migrate.py`. New tests cover recursion, hook
  preservation, frontmatter carryover (rename test), install symlink swap,
  pre-existing-symlink refusal, mutex flag conflict, self-recursion guard,
  symlink-as-file rejection, nested-feedback subdir preservation, and an
  end-to-end "every input byte addressable from new brain" round-trip.

## v0.3 — Episode schema unification + stats subcommand (2026-04-29)

The companion [`agentry`](https://github.com/mustafa-bharmal-shipveho/agentry) integration added two writers (coding sessions + agentry's personal-agent surfaces) on the same brain. To keep their lessons distinct without splitting into separate stores, every episode now carries two new fields and the dream cycle clusters within-stream.

### `origin` + `summary` fields

Every episode written via `sdk.append_episodic` (or the `claude_code_post_tool.py` hook) carries:

- **`origin: str`** — discriminator. `coding.tool_call` for Claude Code post-tool hooks (default — auto-stamped if missing); `agentry.<agent>.<event>` for personal-agent writers; freeform for other frameworks.
- **`summary: str`** — 1-line cluster feature. Auto-derived as `(reflection or action)[:120]` when not explicit. `cluster.py` reads `summary` first, falls back to the legacy `(action, reflection, detail)` triplet — pre-v0.3 episodes cluster identically to before.

`cluster.content_cluster` groups by `origin` before clustering within bucket. Two episodes with identical text but different origins never end up in the same cluster — codex-driven decision after a multi-tenant review caught that `pattern_id` collisions would silently drop one origin's candidate. `pattern_id(claim, conditions, origin)` now mixes origin into the hash unless it's the legacy `coding.tool_call` default (back-compat for already-staged candidates).

Candidates now carry `origin` too (`promote.write_candidates` propagates it), so per-namespace lessons stay traceable to their stream.

### Migrating legacy episodes

A one-shot helper stamps `origin: "coding.tool_call"` on entries written before v0.3:

```bash
# Dry-run first — reports counts without writing
python3 -m agent.tools.backfill_origin --brain-root ~/.agent --dry-run

# Real run (atomic; idempotent; preserves entries that already have an explicit origin)
python3 -m agent.tools.backfill_origin --brain-root ~/.agent
```

Sentinel-locked under `<jsonl>.lock` (matches the dream cycle's contract) so concurrent appends from a live Claude Code session are safe. Reports the count of dropped unparseable lines so operators can decide whether to investigate.

### `sdk_cli stats` subcommand

```bash
$ python3 -m agent.tools.sdk_cli stats --brain-root ~/.agent
{
  "namespaces": ["default", "inbox", "mustafa-agent"],
  "episodeCount": 3712,
  "lessonCount": 20,
  "candidateCount": 4,
  "perNamespace": {
    "default":       {"episodes": 3700, "lessons": 18, "candidates": 2},
    "inbox":         {"episodes":    8, "lessons":  1, "candidates": 1},
    "mustafa-agent": {"episodes":    4, "lessons":  1, "candidates": 1}
  }
}
```

`--namespace NS` to slice. Walks `<brain>/memory/episodic/` and excludes reserved subdirs (`snapshots/`, `working/`, etc.) so a stray jsonl in a kernel-internal dir doesn't leak into the count. The agentry-side `agentry brain stats` CLI is a thin presenter over this output.

## v0.2-rc1 — External-consumer SDK + namespaces (2026-04-29)

External agent frameworks can now read and write the brain through `agent/memory/sdk.py` using namespaces.

### Added

- `agent/memory/sdk.py` — exposes `append_episodic`, `query_semantic`, `read_policy`, `write_policy`, and `register_clusterer`. Each takes a `namespace` arg matching `^[a-z][a-z0-9_-]{0,31}$`.
- `agent/dream/registry.py` — pluggable per-namespace dream-cycle clusterers; `run_all` aggregates results across namespaces.
- `agent/tools/promote.py` and `agent/tools/rollback.py` — manage tier policy + audit log per namespace.
- `--namespace NS` flag on `graduate.py` and `reject.py`.

### Changed

- Backward compatibility: `namespace="default"` maps to the v0.1 paths (no extra subdir under `episodic/`, `semantic/`, `candidates/`). Existing v0.1 brains do not need migration.

### Reference consumer

[`agentry`](https://github.com/mustafa-bharmal-shipveho/agentry) (TypeScript runtime) is the end-to-end SDK consumer. Its `MemoryProvider` interface lets users swap brainstack for any other backend without forking.

## v0.1.1 — Security hardening (2026-04-27)

Applies the priority-2-through-10 findings from `SECURITY_REVIEW.md`. C1
(employer-sensitive content on a personal-account brain remote) is a
deployment decision and is intentionally not addressed by code.

### Added
- `agent/tools/redact_jsonl.py` — sync-time JSONL secret scrubber.
  Walks every string field recursively, replaces secret-shaped
  substrings with `[REDACTED:<pattern_name>]`, rewrites atomically.
  Closes the gap where the post-tool hook captures pre-redaction
  snippets (raw Bash commands, Edit text) into episodic JSONL.
- `agent/memory/_atomic.py` — temp+fsync+os.replace helper.
- `agent/tools/dream_runner.py` — fcntl-based dream cycle launcher.
  Replaces the launchd plist's dependency on the GNU `flock(1)` binary
  (not bundled with macOS).
- `templates/brain-secret-scan.yml` — server-side trufflehog + gitleaks
  GitHub Action; catches `git commit --no-verify` bypasses.
- `tests/conftest.py` — gates hook-precedence tests behind Python ≥ 3.10
  (vendored upstream hook uses 3.10 syntax without `from __future__`).
- New tests: `test_redact_jsonl.py`, `test_atomic.py`,
  `test_dream_runner.py`. 74/74 passing on Python 3.13.

### Changed
- `agent/tools/redact.py` — load `<brain>/redact-private.txt` and merge
  user-supplied regexes (closes the docs-vs-code mismatch where the file
  was created by the installer but never read). Added modern token
  patterns: AWS STS (`ASIA*`), GitHub fine-grained PATs (`github_pat_*`),
  OpenAI project keys (`sk-proj-*`), Anthropic (`sk-ant-*`), Slack
  (`xox*` + webhooks), Stripe live/test/restricted, Sentry DSN, Datadog
  API, Google API keys (`AIza*`), Authorization Bearer/Basic headers,
  PEM/PGP/OpenSSH private key blocks. Added a Shannon-entropy sweep
  (URL-aware so Notion/Drive/GitHub URLs don't false-positive).
- `agent/harness/hooks/agentic_post_tool_global.py` — validate
  `BRAIN_ROOT` resolves under `$HOME` and contains the vendored hook
  script; refuse otherwise (closes the env-poisoning RCE vector). Log
  every `.agent-local-override` fire event to `<brain>/override.log`
  so users notice when logging is silently disabled.
- `agent/memory/auto_dream.py` / `promote.py` / `review_state.py` —
  switch to atomic writes via `_atomic.py`. Closes the SIGKILL-during-
  truncate-and-rewrite torn-file window.
- `agent/tools/sync.sh` — require `trufflehog` *or* `gitleaks` (escape
  hatch: `SYNC_ALLOW_NO_SCANNER=1`). Resolve a Python ≥ 3.10. Python
  `fcntl` fallback when `flock(1)` isn't on PATH. Run the JSONL scrubber
  before staging.
- `install.sh` — `--upgrade` now also syncs `agent/memory/*.py` (was
  silently leaving them stale). Warn if no secret scanner is on PATH.
  Print resolved absolute Python + hook paths so the user pastes
  literals into `~/.claude/settings.json` (no `$HOME` expansion).
- `adapters/claude-code/settings.snippet.json` — placeholders +
  security note explaining why `$HOME` in hook commands is dangerous.
- `templates/com.user.agent-dream.plist` — call `dream_runner.py`
  directly; no shell `flock(1)` dependency.
- `docs/redaction-policy.md`, `docs/git-sync.md`,
  `docs/hook-precedence.md` — updated to match the new behavior.

### Fixed
- redact.py self-bite: the regex pattern definitions for PEM/PGP/SSH
  private keys matched the source file's own regex literals on whole-
  file scan. Inline `# redact-allow` markers on each literal line.

### Persona-agent regression sweep (2026-04-27)

Six parallel persona agents ran against the just-applied v0.1.1 code
and surfaced 24 additional bugs, all fixed within v0.1.1:

**Critical regression** (introduced by the v0.1.1 atomic-write fix and
caught by the race-atomicity persona):
- Lock inversion: `_write_entries_locked` used `os.replace`, which
  swaps the data file's inode and invalidates `flock` held on the
  old inode. Stress test reproduced ~3% silent data loss with 20
  concurrent appenders + 1 dream cycle. **Fix:** switched both
  `_episodic_io.append_jsonl` and `auto_dream._episodic_locked` to
  flock a sentinel sibling (`<jsonl>.lock`), decoupling lock identity
  from data-file inode lifetime. New `tests/test_concurrent_appends.py`
  verifies 100/100 rows survive 20-way contention.

**Red-team bypasses** (12 attacks executed; 7 closed):
- B1 URL userinfo (`https://user:secret@host/`) — added
  `url_userinfo` pattern.
- B2 Base64-encoded secrets in JSONL — added entropy sweep to
  `redact_jsonl.py`.
- B3 `# redact-allow` marker abuse via JSON-string burial — marker
  must now be at line-start or preceded by whitespace.
- B4 ReDoS in user-supplied regex — `load_private_patterns` rejects
  patterns matching nested-quantifier shapes.
- B5 `\b` failed on `MY_TOKEN=` (both `_` and `=` are word chars) —
  added optional `[A-Z][A-Z0-9_]*[_-]` prefix and `[_-][A-Z0-9_]*`
  suffix groups; value capture moved to group 4.
- B9 newer Slack token shapes (`xapp-`, `xoxc-`, `xoxd-`, `xoxe-`).
- B12 multiple distinct secrets per line — drop `break` after first
  hit; track consumed spans to avoid double-reporting overlaps.

**Privacy-audit fixes**:
- `redact-private.txt` self-flag (file lives under scanned root) —
  added explicit skip list to `iter_files`.
- PEM blocks generated triple `high_entropy` hits on inner base64 —
  multi-line scan now records interior line range and entropy sweep
  honors it.
- Added Twilio (`AC*`, `SK*`), SendGrid (`SG.*`), NPM (`npm_*`),
  Mailgun (`key-*`), Heroku patterns.
- Shipped `templates/redact-private.example.txt` seed file.

**Skeptic-codereview fixes**:
- `_log_override_fire` hardcoded `~/.agent` regardless of resolved
  `BRAIN_ROOT`. Test passed by accident on default brain. Now uses
  resolved brain root.
- `_atomic.cleanup_stale_tmp` was dead code — wired into `sync.sh`.
- `redact_jsonl.atomic_write` duplicated `_atomic.atomic_write_bytes`
  — now delegates with a portability fallback.
- `scrub_value` didn't traverse dict keys — now does.
- `test_runner_does_not_call_shell_flock` had a broken short-circuit
  that always passed — replaced with AST-based forbidden-call walk.

**Sysadmin-lifecycle fixes**:
- `sync.sh` aborted fatally on a fresh brain because `data-layer/`
  didn't exist. Build target list dynamically.
- `install.sh --upgrade` `cp` loop died if `memory/` didn't exist
  on a partial brain. `mkdir -p` first.
- `_episodic_io.append_jsonl` raised `PermissionError` traceback per
  tool call when JSONL was read-only. Now degrades silently.

**Portability fix**:
- Vendored `claude_code_post_tool.py` used `re.Pattern | None` syntax
  without `from __future__ import annotations` → crashed on Python
  3.9. Added the future import. Conftest 3.9-skip guard removed.
  96/96 tests pass on both 3.9 and 3.13 now.

## v0.1.0 — Lean MVP + dashboard (2026-04-26)

- `install.sh` targeting `~/.agent/` globally
- Vendored dream cycle from upstream agentic-stack v0.11.2
- Lessons.jsonl schema extension for `why` / `how_to_apply` fields
- Clean-room: `redact.py`, `sync.sh`, `migrate.py`,
  `hooks/agentic_post_tool_global.py`
- Claude Code adapter (settings.json snippet, manual-merge instructions)
- Data-layer dashboard exporter (vendored from upstream)
- Documentation: architecture, memory-model, dream-cycle,
  claude-code-setup, git-sync, redaction-policy, hook-precedence
- Privacy audit (gitleaks + trufflehog + manual `git grep` + fresh-account
  smoke install)

## v0.0.1 — Scaffold (2026-04-26)

- Initial repo skeleton: `tools/`, `hooks/`, `adapters/claude-code/`,
  `schemas/`, `templates/`, `docs/`, `tests/`, `examples/`, `memory_seed/`
- LICENSE (Apache 2.0)
- NOTICE (attribution to codejunkie99/agentic-stack v0.11.2)
- UPSTREAM.md (vendored file inventory, pinned commit, rebase process)
- README.md (pitch + quickstart placeholder)
