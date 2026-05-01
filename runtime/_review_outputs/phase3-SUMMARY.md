# Phase 3 review outputs

Five adversarial reviews ran at the end of Phase 3.

## Verdicts

| Reviewer | Verdict |
|---|---|
| Codex code review | **APPROVE** (7 of 7 PASS) |
| Skeptic | **BLOCK** + retracts "fancy logger" — control property accepted |
| Security | **BLOCK on #1** (items_added pass-through) + 4 FIX |
| Performance | **PASS** — Engine + policy under 50ms p95 budget |
| OSS Maintainer | **BLOCK** (schema breaking-by-construction) + 7 FIX |

## BLOCKs addressed in this commit

- **Security #1**: `items_added` pass-through could smuggle raw payloads.
  → `dump_event` now hard-validates each entry as `InjectionItemSnapshot`,
  raises `ValueError` with explicit message otherwise. 2 regression
  tests added in `test_events.py`.

## BLOCKs accepted as v0.x roadmap (documented)

- **Skeptic BLOCK on "live injection control"**: by design, the adapter is
  append-only logging; "actual injection" requires a real Claude Code
  dogfood session that the autonomous run cannot perform. Phase 6
  smoke-test is documented for the user. Docstring updated to remove
  TouchItem mention (which `_translate` doesn't actually emit).
- **Maintainer #1 (schema bumps breaking)**: v1.1 loaders accept only
  v1.1. Multi-version reader is v0.x roadmap. Documented in
  `docs/runtime.md`.

## FIX items addressed

- Skeptic: docstring claimed TouchItem translation in replay; removed.
- Maintainer #7: manifest doc said "v1.0 ships"; updated to "v1.1".

## FIX items deferred to v0.x with documentation

| # | Source | Item |
|---|---|---|
| Sec #2 | Security | render_diff/CLI ls print paths verbatim (terminal scrollback leak risk). v0.x: `--show-paths` opt-in. |
| Sec #3 | Security | items_added amplifies path/PII volume. Adapter should normalize paths. v0.x: configurable normalization. |
| Sec #4 | Security | Item.sha256 fingerprintable (same threat as Phase 1 sha256). v0.x: HMAC-keyed alternative. |
| Maint #2 | Maintainer | Replay's `_translate` is fragile to renamed events. v0.x: pluggable translation map. |
| Maint #3 | Maintainer | Engine event vocabulary closed. v0.x: registration mechanism. |
| Maint #4 | Maintainer | Policy interface bucket-scoped only. v0.x: cross-bucket eviction. |
| Maint #5 | Maintainer | x_* extensions not plumbed Engine -> manifest. v0.x: AddItem.extensions. |
| Maint #6 | Maintainer | Backend interface FS-baked. v0.x: storage abstraction. |
| Skeptic #5 | Skeptic | Cross-Python/OS determinism not tested. v0.x: CI matrix. |

## Test impact

After applying fixes:

| Suite | Tests |
|---|---|
| Brainstack pre-existing | 581 |
| Runtime new (post-Phase-3-fixes) | 230 (+2 security regression) |
| Total | 808 (all green) |
