# STATUS

Long autonomous run on `mustafa/runtime-v0`. v0.2 context runtime.

## Current state

- **Phase:** Night 3 wrap-up (Phase 6 final reviews running)
- **State:** GREEN-3 pending final review verdicts
- **Branch:** `mustafa/runtime-v0` (19 commits ahead of `main`)
- **Last tag:** `phase-3-reviews` (final commit will be `night-3-final` once reviews land)

## Phase progress

- [x] Setup — feature branch + runtime/ skeleton
- [x] 0a (built; user runs harness for empirical data)
- [ ] 0b (depends on 0a — user action)
- [x] 0c — concurrent-hook flock smoke test (5 tests)
- [x] 0d — phase0-empirical.md template + handoff
- [x] 1a — manifest schema v1.1 (16 tests)
- [x] 1b — TokenCounter Protocol + offline default (10 tests)
- [x] 1c — Policy + LRU/recency/pinned-first (25 tests)
- [x] 1d — event log schema v1.1 + data policy (13 tests)
- [x] 2c — synthetic test battery (68 tests)
- [x] 3a — runtime/core/locking.py (9 tests)
- [x] 3b — schema bump 1.0->1.1 + items_added (2 tests)
- [x] 3d — Engine state machine (17 tests)
- [x] 3f — replay engine + diff (9 tests)
- [x] 3g/h — integration + control property (3 tests)
- [x] Phase 3 reviews — 5 codex+personas; BLOCKs addressed
- [x] 4a/4b — Claude Code adapter + CLI (28 tests)
- [x] 4d — performance micro-benchmarks (8 tests)
- [x] 5a-e — README + docs/runtime.md + CHANGELOG + version bump
- [→] 6a-d — final review + PR draft (running)

## Test counts

| Suite | Tests |
|---|---|
| Brainstack pre-existing | 581 |
| Runtime new | 230 |
| **Total green** | **808** |

Latest `pytest -q` run: 808 passed in 136s. No regressions.

## Tags walked through

```
night-1-handoff        — Phase 0+1+2 spec done, 730 tests
night-1-reviews        — codex+skeptic+security applied, +sha256 default-off
night-1-final          — STATUS reflects post-review state

subphase-3a-locking    — atomic primitives, +9 tests
subphase-3b-schema-1.1 — items_added in events, +2 tests
subphase-3d-engine     — Engine state machine, +17 tests, control property
subphase-3f-replay     — replay + diff, +9 tests
subphase-3g-integration— byte-equal live↔replay, +3 tests

subphase-4ab-adapter   — claude_code adapter + CLI, +28 tests
subphase-4d-perf       — perf micro-benchmarks, +8 tests
subphase-5-docs        — README + docs/runtime.md + CHANGELOG + 0.2.0
phase-3-reviews        — items_added security validation
```

## Final commit (when reviews land)

`night-3-final` will be tagged after Phase 6 reviews are processed and any
final fixes applied. The PR description draft lives at
`runtime/_review_outputs/PR_DRAFT.md`.

## Honest scope

The runtime owns the **injection layer**, not the model's KV cache.
"Eviction" = "demotion-from-injection on the next turn." See
`docs/runtime.md` and `runtime/_empirical/data-policy.md` for the threat
model and v0.x roadmap.

## What you do when you wake up

1. Read `runtime/_review_outputs/PR_DRAFT.md` — that's the PR description
   ready to push.
2. Run the hook telemetry harness to fill in Phase 0 empirical answers
   (one command in `HALT.md`).
3. Capture a real session for the README demo block (replace the synthetic
   example).
4. `git push origin mustafa/runtime-v0` and open the PR using the draft
   description.
