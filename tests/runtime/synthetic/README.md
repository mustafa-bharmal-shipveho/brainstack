# Synthetic test battery (sub-phase 2c)

Adversarial test generators that run on every commit. Each one targets a
specific failure mode the runtime must defend against. Synthetic-test
failure = halt.

| Category | File | Status |
|---|---|---|
| Data leak | `test_leak_battery.py` | here |
| Concurrent burst | `tests/runtime/test_harness_concurrent_flock.py` | already covered |
| Schema evolution | `tests/runtime/test_manifest.py`, `test_events.py` | already covered |
| Policy determinism | `test_policy_determinism_stress.py` | here |
| Budget overflow | `test_budget_overflow.py` | here |
| Token counter accuracy | `tests/runtime/test_tokens.py` | already covered |
| Timestamp independence | `test_timestamp_independence.py` | here |
| Path normalization | `test_path_normalization.py` | here |

The "already covered" rows aren't lies of omission — see those test files.
We don't duplicate; we annotate.
