# Phase 0: Empirical findings

This is a partial report. Sub-phases 0a (hook telemetry) and 0b (tool-event
payload sampler) require running real Claude Code sessions, which exceeded
the autonomous-run authorization. Sub-phase 0c (concurrent flock) ran
unattended and is complete.

The user's morning action: run `bash runtime/_empirical/harness/run_synthetic_sessions.sh 50 mixed` then re-aggregate. See `HALT.md` for the full handoff.

---

## 0a — Hook telemetry: PENDING USER

### Built (autonomous)

- `runtime/_empirical/harness/settings.json` — overlay registering 9 hook events (SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Notification, Stop, SubagentStop, PostCompact, PostToolUseFailure)
- `runtime/_empirical/harness/hooks/log_event.sh` — generic logger
- `runtime/_empirical/harness/hooks/_atomic_append.py` — POSIX-fcntl-locked appender (replaces bash flock; macOS doesn't ship it by default)
- `runtime/_empirical/harness/aggregator.py` — computes per-event deliverability rates from `events.jsonl` + `expected_runs.json`
- `runtime/_empirical/harness/run_synthetic_sessions.sh` — fires N short `claude --print` sessions with the harness settings overlay

### What runs autonomously

The harness scripts and aggregator have unit tests (`tests/runtime/test_harness_concurrent_flock.py`, all green). What does NOT run autonomously is the actual claude-spawning loop — `--permission-mode auto` invocations are scope escalation under unattended execution.

### What the user runs

```bash
cd ~/Documents/brainstack
bash runtime/_empirical/harness/run_synthetic_sessions.sh 50 mixed
python3 runtime/_empirical/harness/aggregator.py --expected expected_runs.json
```

Then paste the markdown table into the **Findings** section below and decide go/no-go on Phase 1.

### Findings

> **TBD — pending 50-session telemetry pass by user.**
> Pass condition: SessionStart, UserPromptSubmit, PostToolUse, Stop each ≥90% deliverability. Below that, the runtime needs a different injection mechanism (UserPromptSubmit-based re-injection rather than SessionStart cat).

### Decision

> **TBD — pending findings above.**

---

## 0b — Tool-event payload sampler: PENDING USER

### What it produces

The same harness writes `_data/payload-samples.jsonl` (gitignored) with full stdin payloads from each hook firing. The user reviews this file *locally* (it may contain content from synthetic prompts) to determine:

1. What fields PostToolUse exposes for each tool (file content vs metadata)
2. Whether SessionStart's payload contains the session id and any other useful fields
3. Whether there is a documented `PostCompact` event in current Claude Code versions, or if the field is silent

### Schema doc to produce

After the 50-session pass, write `runtime/_empirical/payload-schema.md` documenting (with examples redacted) the fields exposed for each event. This feeds sub-phase 1a finalization (the manifest's tool-event-specific fields, currently reserved as `x_tool_*`).

### Findings

> **TBD — pending payload review by user.**

---

## 0c — Concurrent-hook flock smoke test: COMPLETE

### Setup

`tests/runtime/test_harness_concurrent_flock.py`:
- 5 tests
- 20 hooks fire in parallel via Python `concurrent.futures.ThreadPoolExecutor`
- Each hook calls `log_event.sh` with a unique payload
- After all 20 complete, assertions:
  - exactly 20 lines in `events.jsonl`
  - every line is valid JSON
  - all 20 distinct `run_tag` values present (no overwrites)
  - `.write.lock` is a separate file from `events.jsonl`
  - empty stdin produces a metadata row but no payload-samples row
  - **fake-secret leak test:** payload containing `sk_live_FAKE_TEST_TOKEN_DO_NOT_LEAK_*` does NOT appear in `events.jsonl`

### Findings

| Test | Result |
|---|---|
| 20 concurrent hooks → 20 valid JSON lines, no corruption | PASS |
| All 20 distinct run_tags present | PASS |
| Sentinel lock != data file | PASS |
| Empty stdin → metadata-only, no payload row | PASS |
| Fake-secret pattern not in events.jsonl | PASS |

### Decision

The flock-via-Python-fcntl pattern works. macOS-native bash without `flock` was a bug initially; the Python helper resolves it portably.

This validates the sentinel-vs-data-file pattern that brainstack already uses in `_atomic.py` and confirms the runtime's eventual `runtime/core/locking.py` (sub-phase 3a) can inherit the same approach.

---

## 0d — Final go/no-go: PENDING

Will be filled in after 0a and 0b complete. Skeleton:

> Phase 1 is gated on:
> - SessionStart, UserPromptSubmit, PostToolUse, Stop each ≥90% deliverability (0a)
> - PostToolUse exposes enough payload to compute token counts (either content or path+slice metadata) (0b)
> - PostCompact: documented as v0.2 deferred regardless of presence
>
> If 0a passes but 0b shows we only get tool metadata (no content), the manifest's token-counting strategy shifts to "read the file ourselves at hook time" rather than "consume what PostToolUse gave us." That is a 1-day adapter change in Phase 4, not a thesis-breaker.
>
> If 0a fails (<90% on SessionStart), we redesign the runtime's session-bootstrap path to use UserPromptSubmit re-injection instead. Halt and spec.
