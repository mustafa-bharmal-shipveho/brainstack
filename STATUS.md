# STATUS

Long autonomous run on `mustafa/runtime-v0`. Updated after every sub-phase.

## Current state

- **Phase:** Night 1 complete
- **State:** YELLOW-1 (Phase 1 + 2c complete with codex APPROVE; Phase 0 partially blocked on user-triggered Claude sessions; persona BLOCKs addressed in code or documented as Phase 3+4 work)
- **Last commit:** `26bf25b runtime(reviews): apply codex + persona findings`
- **Last tag:** `night-1-reviews`
- **Branch:** `mustafa/runtime-v0` (9 commits ahead of `main`)
- **Open question:** see `HALT.md` — user runs the harness in the morning, decides go/no-go on Night 2

## Plan reference

See `~/.claude/plans/i-ran-into-this-buzzing-rabbit.md` for the full plan,
thesis, sub-phase breakdown, persona schedule, and synthetic test catalogue.

## Sub-phase progress

- [x] Setup — feature branch + runtime/ skeleton
- [x] 0a (built) — hook telemetry harness — execution blocked, awaits user
- [ ] 0b — tool-event payload sampler (depends on 0a)
- [x] 0c — concurrent-hook flock smoke test
- [ ] 0d — phase0-empirical.md writeup + go/no-go (template in place; user fills in 0a/0b sections after running harness)
- [x] 1a (partial) — manifest schema v1.0 (tool-specific fields reserved as `x_tool_*` pending 0b)
- [x] 1b — TokenCounter Protocol + offline default
- [x] 1c — Policy Protocol + LRU/recency/pinned-first defaults
- [x] 1d — event log schema + data-policy doc
- [ ] 2a — record-mode adapter (depends on 0a/0b)
- [ ] 2b — record real fixtures (depends on 2a)
- [x] 2c — synthetic test battery (5 of 8 categories; 3 already covered elsewhere)
- [x] codex review (Phase 1 + 2c diff)
- [x] persona reviews — Skeptic + Security
- [ ] 3a-3h — Core impl (Night 2)
- [ ] 4a-4d — Adapter + dogfood (Night 3)
- [ ] 5a-5e — Docs (Night 3)
- [ ] 6a-6d — Review + PR (Night 3)

## Test counts (post-review)

| Suite | Tests |
|---|---|
| Brainstack pre-existing | 581 |
| Runtime new (parametrize-expanded) | 154 (+23 from review fixes) |
| **Total green** | **730** |

Latest `pytest -q` run: 730 passed in 134.05s. No regressions.

## LOC

```
runtime/core/         ~1.0k LOC
tests/runtime/        ~1.5k LOC + ~200 LOC docs/policy
runtime/_empirical/   ~400 LOC harness + ~200 LOC docs
TOTAL                 ~3.3k (under 2k production / 500 test budget for runtime/core/, runtime/adapters/)
```

Production LOC under `runtime/core/` is ~1.0k — well under the 2.0k v0 cap.

## Night 1 endpoint adjustment

The original plan said "Night 1 endpoint = end of Phase 2." Reality:
- Phase 0 is half done (0a built but unrun, 0c green, 0b/0d pending user)
- Phase 1 fully done with quality
- Phase 2c done; 2a/2b need the empirical answers first

Net: I am AHEAD on Phase 1 quality and BEHIND on Phase 2 fixtures (which
need real Claude sessions). Total deliverable for Night 1 is comparable
to plan, just with a different shape.

## How to resume after compaction

1. Re-read this file.
2. `git -C ~/Documents/brainstack log --oneline -20` for commit history.
3. `git -C ~/Documents/brainstack tag -l 'subphase-*'` for sub-phase tags.
4. Read the most recent sub-phase output file.
5. Read `HALT.md` for the user-action handoff.

## Reviews

Three `codex exec` calls completed. Findings + applied fixes are in
`runtime/_review_outputs/SUMMARY.md`. Verdicts:

- Codex code review: **APPROVE** (7 of 7 checks pass)
- Skeptic persona: **BLOCK** — 8 findings; conceptual ones (Phase 1
  alone is a logger) are by design; structural ones (#3 score in
  snapshot, #4 per-item x_*) fixed in code; doc tightening landed
- Security persona: **BLOCK** — 7 findings; the two BLOCKs (sha256
  fingerprint, x_* extension bypass) fixed: sha256 default-off,
  MAX_EXTENSION_BYTES=1024 guard

Tag: `night-1-reviews`.
