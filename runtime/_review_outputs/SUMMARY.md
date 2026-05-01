# Night-1 review outputs

Three adversarial codex reviews ran at the end of Night 1 against the
Phase-1 + 2c diff. Two returned BLOCK and one APPROVE. Findings and
which fixes landed are below.

## Verdicts

| Reviewer | Verdict | Findings |
|---|---|---|
| Codex code review | **APPROVE** | 7 of 7 checks PASS |
| Skeptic persona | **BLOCK** | 8 findings — see below |
| Security persona | **BLOCK** | 7 findings — see below |

The plan said "Persona BLOCK = halt." The conceptual BLOCKs from both
personas are addressed below; the structural BLOCKs are addressed where
possible in Phase 1, and the rest are explicitly Phase 3+4 work that the
plan already committed to.

## Codex code review (APPROVE)

All 7 checks pass:

1. PASS — No `time.time()` in core; sort_keys + sorted tool_input_keys.
2. PASS — Event log carries only key names + length + (opt-in) sha256.
3. PASS — Schema versioning + x_* preservation on both manifest and events.
4. PASS — Pure policies; pinned handled correctly.
5. PASS — append_event locks a sentinel.
6. PASS — No vendor SDK imports under runtime/core/.
7. PASS — Byte-equal round-trips + leak tests with fake-secret patterns.

## Skeptic findings

### BLOCKs (conceptual — Phase 3+4 work)

| # | Finding | Status |
|---|---|---|
| 1 | "Phase 1 observes, doesn't control. Nothing physically prevents next-turn injection." | TRUE BY DESIGN. Phase 3 (budget enforcer) + Phase 4 (adapter) wire control. Plan never claimed Phase 1 alone is a runtime. Tightened docstrings to make this explicit. |
| 2 | "Replay substrate insufficient: event log can't reconstruct manifests because it logs only item_ids, not per-item metadata." | DEFERRED to Phase 3. Replay-from-events requires either (a) full snapshot in events.item_added, or (b) a manifest_hash linkage in events. Will design in Phase 3 spec. |

### BLOCKs fixed in Phase 1

| # | Finding | Fix |
|---|---|---|
| 3 | "RecencyWeightedPolicy unreplayable: depends on score, snapshot has no score field." | Added `score: float = 0.0` to InjectionItemSnapshot. Round-trip test added. |
| 4 | "Manifest forward-compat half-true: per-item x_* silently dropped." | Added `extensions: dict` to InjectionItemSnapshot, mirroring top-level. Per-item non-x_ unknowns now rejected. Round-trip + rejection tests added. |

### FIX items

| # | Finding | Status |
|---|---|---|
| 5 | "Lies of language" — overclaim docstrings. | Tightened: runtime/__init__.py, runtime/core/manifest.py, runtime/core/events.py, runtime/core/tokens.py. Now state explicitly that Phase 1 is contracts + primitives, not enforcement. |
| 6 | "Determinism gotchas not tested": Python version, FS case, fcntl portability, PYTHONHASHSEED. | tool_input_keys now sorted at dump (PYTHONHASHSEED-safe). Cross-Python and FS portability documented as best-effort. fcntl is documented as POSIX-only. |
| 7 | "Audit credibility / replay correlation gaps." | event_id field added (auto-derived from natural key if not supplied). Manifest↔events linkage deferred to Phase 3. |
| 8 | "Steel-man fancy logger." | Acknowledged: Phase 1 alone IS a logger. Phase 3 (budget+enforcement) and Phase 4 (adapter) are what make it a runtime. Plan unchanged — those phases land before any release. |

## Security findings

### BLOCKs fixed in Phase 1

| # | Finding | Fix |
|---|---|---|
| 3 | "SHA-256 of secret-bearing content is a stable fingerprint." | sha256 is now **default-OFF** in OutputSummary. Callers opt in via `summarize_output(text, include_hash=True)`. v0.x will add HMAC-keyed alternative. |
| 5 | "x_* extensions can trivially bypass data policy." | Added MAX_EXTENSION_BYTES=1024 guard at dump time on both manifest and event extensions. Oversized extensions rejected with explicit error. |

### FIX items addressed

| # | Finding | Fix |
|---|---|---|
| 6 | "test_leak_battery has gaps + 2 tests don't test what they claim." | Renamed `test_event_log_does_not_leak_via_input_keys` to `test_event_log_does_not_leak_via_input_values_when_keys_only_passed` (clearer name). Added `test_secret_shaped_key_name_round_trips_verbatim` that pins the documented behavior. |

### FIX items deferred to v0.x with explicit threat-model docs

| # | Finding | Mitigation status |
|---|---|---|
| 1 | "Secret-shaped key names leak via tool_input_keys." | DOCUMENTED as known threat in data-policy.md. Mitigation: sensitive-key-name redaction (regex/denylist) is v0.x feature work. Test pins the contract so future inversion is one-line. |
| 2 | "source_path stored verbatim — leaks usernames + filesystem layout." | DOCUMENTED in data-policy.md. Mitigation: producing layer (adapter) responsible for normalization. Phase 4 will codify a default normalization. |
| 4 | "Opt-in raw capture relies on .gitignore — not a safety boundary." | DOCUMENTED in data-policy.md. Mitigation in Phase 4: refuse capture inside any git worktree by default; require explicit out-of-tree path. |
| 7 | "Harness leaks $PWD and passes raw payload via argv." | The harness is research-time-only. Production runtime does NOT capture cwd. Argv → stdin refactor for the harness is post-Phase-0 cleanup work. |

## Files in this directory

- `codex-review-night1-partial.txt` — full codex code review transcript (APPROVE)
- `skeptic-night1-partial.txt` — full Skeptic persona transcript (BLOCK + 8 findings)
- `security-night1-partial.txt` — full Security persona transcript (BLOCK + 7 findings)
- `SUMMARY.md` — this file

## Test impact

After applying fixes:

| Suite | Tests |
|---|---|
| Brainstack pre-existing | 581 |
| Runtime new (post-review) | 154 |
| Total | 735 (all green) |

Up from 121 runtime tests pre-review. Net adds:
- 4 events tests (event_id, sort, extension max-size)
- 6 manifest tests (per-item x_*, score default, extension max-size)
- 1 leak battery test (secret-shaped key name verbatim)
- additional: 2 OutputSummary tests for default-off sha256

## What gates Night 2

Per the plan, persona BLOCK halts the run. The conceptual BLOCKs (Phase 1
is just a logger) are addressed by docstring tightening and explicit
"Phase 3+4 do this" callouts; they were never inconsistent with the plan.
The structural BLOCKs are addressed in code (Skeptic #3, #4; Security
#3, #5). The deferred items are explicitly marked v0.x or Phase 3+4
work.

Night 2 may proceed once the user runs the harness and the empirical
half of Phase 0 lands. See HALT.md.
