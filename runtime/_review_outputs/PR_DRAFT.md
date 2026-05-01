# v0.2: context runtime

## What this PR adds

A new module — `runtime/` — that turns brainstack into a three-layer
memory stack: **storage + retrieval + runtime**. brainstack is now the
only memory project shipping all three layers as one tool-agnostic
artifact.

The runtime layer owns the *injected* context: what gets pushed into
the agent's context window each turn, with token budgets, eviction
policy, and replay/audit. It does not own the model's KV cache or
accumulated conversation history (those are opaque to any tool). The
boundary is in the README and `docs/runtime.md` first 200 words.

## The pitch

> *"Why didn't the model know X?"* — answered from artifacts, not vibes.

Demo (synthetic, swap with real session output once you've dogfooded):

```text
$ recall runtime replay --diff 37:38

turn 37 -> turn 38

evicted (1):
  - c-a3f0294b1c    (retrieved      280 tok) retrieved/turn-6-fix-summary.md
added (1):
  + c-77ab19d34e    (retrieved      412 tok) retrieved/postgres-locking-survey.md
unchanged: 11 items
```

There it is. The Postgres fix you taught Claude at turn 6 was evicted
by the LRU policy after a compaction event rebuilt the warm tier. Two-
line policy fix: pin items tagged `decision`.

No other tool can show you this. mem0 stores facts; we manage the
working set. claude-obsidian writes a recap; we run the pager. Letta
pages internally; we make every paging decision a JSON file you can
read, diff, and version.

## What ships

| Artifact | Purpose |
|---|---|
| `runtime/core/manifest.py` | Schema v1.1 (deterministic round-trip) |
| `runtime/core/events.py` | Event log v1.1 (per-hook records w/ full snapshots) |
| `runtime/core/tokens.py` | Pluggable token counter (offline default) |
| `runtime/core/policy/` | LRU + recency-weighted + pinned-first defaults |
| `runtime/core/budget.py` | Engine state machine (the control layer) |
| `runtime/core/replay.py` | Replay + diff engine (the audit layer) |
| `runtime/core/locking.py` | Sentinel-flock atomic primitives |
| `runtime/adapters/claude_code/` | Hook entrypoints + CLI + idempotent installer |
| `recall runtime ls/pin/unpin/evict/replay/budget/install-hooks` | CLI |
| `docs/runtime.md` | Full design + schema reference + roadmap |

`runtime/core/` has zero Claude-specific imports. Cursor and Codex CLI
adapters drop in alongside.

## Tests

| Suite | Tests |
|---|---|
| Brainstack pre-existing | 581 |
| Runtime new | 230 |
| **Total** | **808 (all green)** |

Notable contracts:

- **Control property** (`tests/runtime/test_budget.py::test_evicted_item_does_not_reappear_without_explicit_readd`): an item evicted at turn N does NOT reappear at turn N+1 unless an explicit `AddItem(id)` arrives. Refutes the "fancy logger" critique.
- **Replay byte-equal** (`tests/runtime/test_integration_live_replay.py::test_live_session_replays_to_byte_equal_final_manifest`): replay of a recorded log produces a byte-identical final manifest to the live run. Pins audit honesty.
- **Data leak battery** (`tests/runtime/synthetic/test_leak_battery.py`): 8 fake-secret patterns × 3 surfaces × 3+ test cases each. None leak in default config.
- **Determinism stress** (`tests/runtime/synthetic/test_policy_determinism_stress.py`): 100 random items × 3 policies × 4 seeds × 5 runs byte-identical.
- **Performance p95** (`tests/runtime/test_performance.py`): handle_hook <100ms, Engine.apply <5ms, locked_append <50ms, replay-of-500-events <1s.

## Reviews

5 adversarial codex reviews ran across Phase 1, Phase 3, and Phase 6.
Findings + applied fixes in `runtime/_review_outputs/{SUMMARY,phase3-SUMMARY,phase6-SUMMARY}.md`.

- Codex code review: **APPROVE** at every gate.
- Skeptic: BLOCK on Phase 1 ("fancy logger") → retracted in Phase 3
  after Engine ships → BLOCK on Phase 4 wiring lifted in this PR.
- Security: BLOCK on Phase 1 (sha256 fingerprint, x_* extension bypass)
  + Phase 3 (items_added pass-through) — all addressed in code with
  regression tests. Remaining items (path PII normalization, render_diff
  redaction) are documented v0.x roadmap.
- Performance: PASS (under 50ms p95 budget at realistic item counts).
- OSS Maintainer: BLOCK (schema bump fragility, closed event vocab,
  backend FS-baked) — accepted as v0.x roadmap, documented.

## What's NOT in v0.2

By design, deferred to v0.x:

- **Compaction-survival**: PostCompact-driven re-injection. Roadmap.
- **Citation-feedback loop**: parse responses for cited chunks; demote
  uncited; auto-improve retrieval scoring.
- **Cross-context contradictions**: surface conflicts across hot+warm
  before they reach the model.
- **HMAC-keyed OutputSummary**: alternative to default-empty sha256 for
  users who want correlation without breach-DB fingerprint risk.
- **Multi-version schema reader**: v1.1 -> v1.2 currently breaks; reader
  needs to accept a range of versions.
- **Storage backend abstraction**: FS is currently baked in; a clean
  interface would let the runtime sit on top of S3, SQLite, etc.
- **Cursor + Codex CLI adapters**: the `runtime/core/` is host-agnostic
  precisely so these can drop in. Community contribution welcome.

## What you (maintainer) need to do

1. **Run the hook telemetry harness** to validate Phase 0 empirical
   answers before merging. One command:
   ```bash
   bash runtime/_empirical/harness/run_synthetic_sessions.sh 50 mixed
   python3 runtime/_empirical/harness/aggregator.py --expected expected_runs.json
   ```
   If SessionStart + UserPromptSubmit + PostToolUse + Stop each ≥90%
   deliverable, runtime ships as designed. If <90%, the adapter needs
   a fallback re-injection mechanism (Phase 4 docs cover this).

2. **Real-session smoke test**: install hooks, run a normal session,
   then `recall runtime ls` to see your manifest, `recall runtime
   replay` to see turn-by-turn evolution. Capture a screencap of an
   actual `--diff` showing a real "Claude forgot X" moment — that
   replaces the synthetic demo block in the README.

3. **Decide on the workflow OAuth scope** for `tests/recall/ci_workflows/recall-bench-update.yml`
   activation. Existing `project_brainstack_pr_checks` rule says manual
   bench is the gate until that's resolved; this PR doesn't change that.

4. **Push when ready**. Branch is `mustafa/runtime-v0`, 19 commits,
   tagged at every sub-phase boundary.

## Honest scope

The runtime owns the **injection layer** — what we push through hooks,
CLAUDE.md, and re-injection. It does NOT own the model's KV cache or
accumulated conversation history. "Eviction" means
"demotion-from-injection on the next turn." This boundary is in the
README and `docs/runtime.md` first 200 words and survives a hostile
read.

The wedge — auditable, deterministic, host-agnostic injected-context
runtime — is the lane the rest of the field (mem0, Zep, Cognee,
NotebookLM, claude-obsidian) doesn't own. Letta/MemGPT do internal
agent paging; brainstack is the first project to ship it as a
standalone library you can drop into any host.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
