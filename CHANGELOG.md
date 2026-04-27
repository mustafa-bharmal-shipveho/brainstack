# Changelog

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
