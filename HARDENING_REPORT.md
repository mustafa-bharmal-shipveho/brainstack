# v0.6.0 Hardening Report

**Goal:** harden v0.6.0 for company-wide sharing tomorrow.
**Branch:** `harden/v0.6.0-overnight`
**PR:** https://github.com/mustafa-bharmal-shipveho/brainstack/pull/56

## TL;DR

- **1 real bug found and fixed**: `--yes` parser precedence — the new `--yes` case never fired because the existing uninstall `-y|--yes` case shadowed it. So `--yes --no-prompt` together always took the no-prompt path and never auto-accepted migrate prompts. Fixed in commit `1659b60`.
- **23 hardening tests added** across 4 rounds. All passing.
- **Full suite**: 1567 pass + 3 pre-existing flakes unrelated to install (see below). Net delta from my work: +23 tests, 0 regressions.
- **Live smoke test** on user's real machine: uninstall preserved 200 MB memory; reinstall hit status path correctly; catch-up command re-wired all 4 host surfaces; `recall doctor` reports v0.6.0.
- **Recommendation:** ship. PR #56 covers the bug fix + tests.

## What got tested

### Round 1: Defaults flip happy path (in PR #55, already merged)
14 tests pinning the v0.6.0 default-on behavior + each opt-out + upgrade-mode regression + idempotency + summary block UX.

### Round 2: Prepopulated host sources + multi-source discovery
- `--yes` migrates real `~/.claude/projects/<slug>/memory` (no symlink swap, inode preservation)
- Empty `~/.claude` dir: no candidates found, install completes
- No host dirs at all: graceful zero-candidate path
- Claude + Codex + Cursor all populated: all 3 preserved (inodes unchanged)

### Round 3: Content validation — not just status strings
- `enable_auto_recall=true` actually written to `runtime/pyproject.toml`
- `--no-auto-recall` keeps the flag false
- CLAUDE.md sentinel block contains non-trivial directive (>100 chars between sentinels)
- LaunchAgent plists have no unexpanded `REPLACE_HOME` / `REPLACE_PYTHON`

### Round 4: Reentry + static invariants
- `--setup-X` after `--remove-X` re-creates cleanly
- `enable / disable / enable` auto-recall TOML flag flips cleanly
- All 5 opt-out flags have explicit parsers in install.sh
- `-y|--yes` case sets BOTH `UNINSTALL_YES` and `ASSUME_YES` (guards the precedence fix)
- CHANGELOG has v0.6.0 entry
- README has `## Customize your install` H2 with all 5 opt-outs

### Misc
- `--help` still works after parser changes
- Unknown flags still rejected by catch-all
- HOME path with a space character: install completes
- Default install run twice in a row: second hits status path, no surprises

## Bugs found and fixed

### B1 (P0): `--yes` parser shadowed by uninstall `-y|--yes`
- **Symptom:** `./install.sh --yes --no-prompt` always declined migrate prompts (took the `--no-prompt` branch). `--yes` was effectively dead code in install mode.
- **Root cause:** option parser hit the `-y|--yes` case at line 207 (set up earlier for uninstall confirmation) and never reached the new `--yes` case I added at line 331.
- **Fix:** merged the cases. `-y|--yes` now sets BOTH `UNINSTALL_YES=1` (uninstall) and `ASSUME_YES=1` (migrate auto-accept). Removed the duplicate `--yes` case.
- **Pinned by:** `TestFlagPrecedence::test_yes_overrides_no_prompt` + `TestStaticInstallShInvariants::test_yes_is_dual_purpose`.

### B2 (cosmetic, NOT fixed): uninstall dry-run incomplete preview
- **Symptom:** `./uninstall.sh --dry-run` previews `dream` + `sync` plists but actual uninstall ALSO removes `auto-migrate` + `claude-extras` plists. Mismatch between dry-run and live behavior.
- **Risk:** low — the live uninstall does the right thing. The dry-run just under-reports.
- **Not in this PR** — pre-existing in 0.5.0.
- **Recommended follow-up:** open issue for uninstall dry-run accuracy.

## Pre-existing flakes (NOT caused by this branch)

Run against `main` (no hardening changes) — these 3 fail identically:

```
tests/recall/test_adversarial.py::TestJsonSerialization::test_date_value_is_serialized
tests/recall/test_query_fixtures.py::test_query_top_3_bm25_only_lexical[python global interpreter lock]
tests/recall/test_query_fixtures.py::test_query_top_3_bm25_only_lexical[rust ownership lifetime]
```

Out of scope for install hardening. Triage separately.

## What is NOT covered yet

Honest list of remaining gaps if you want to keep pushing:

1. **End-to-end recall query post-install**: install → `recall query "..."` returns results. Currently tested only via `recall doctor` reports clean. The full retrieval pipeline isn't exercised in CI tests.
2. **Multi-platform**: Linux box behavior. The code has `[ "$(uname -s)" != "Darwin" ]` guard but the test mocks aren't exhaustive.
3. **Concurrent installs**: two `install.sh` racing. Probably fine (file-level idempotency), but not tested.
4. **Network failures during `--push-initial-commit`**: install completes, push fails — does the user get a useful error?
5. **`--purge-data` uninstall path**: removes ~/.agent entirely. Not currently tested.
6. **Live `recall query` against the populated brain**: smoke test from your real machine passed; no unit-level coverage.

## Files changed on `harden/v0.6.0-overnight`

```
install.sh                          | small: --yes dual-purpose merge
tests/test_install_hardening.py     | new: 23 scenarios, 4 rounds
HARDENING_REPORT.md                 | this file
```

## Recommendation

**Ship PR #56.** The bug fix is needed for correctness. The tests pin behavior for future contributors. The pre-existing flakes are unrelated and should be triaged in a separate issue.

After merge:
- Tag `v0.6.1` (patch — bug fix only, no new public behavior)
- Share with the team
- The 6 "not covered yet" gaps are nice-to-have, not ship-blockers
