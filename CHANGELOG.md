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
