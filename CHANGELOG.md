# Changelog

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
