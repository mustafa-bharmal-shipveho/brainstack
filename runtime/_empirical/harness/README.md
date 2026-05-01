# Hook telemetry harness (sub-phase 0a)

Empirical research artifact for the runtime module's Phase 0. Measures the
deliverability of every Claude Code hook event under realistic workloads.
Output feeds `phase0-empirical.md` and gates the rest of the runtime work.

## What it measures

- Per-event deliverability (`SessionStart`, `UserPromptSubmit`, `PreToolUse`,
  `PostToolUse`, `Notification`, `Stop`, `SubagentStop`, `PostCompact`,
  `PostToolUseFailure`).
- Per-tool `PostToolUse` counts (`Read`, `Glob`, `Grep`, `Bash`, `Edit`, `Write`).
- Whether `PostCompact` is a real event delivered by Claude Code today.
- Approximate payload size per event type (informs `runtime/core/budget.py`).

## What it does NOT capture by default

- File contents read by `Read`. The hook records *that* a `Read` happened and
  the payload key list — not the file body. The full stdin payload is logged
  to `_data/payload-samples.jsonl` for sub-phase 0b analysis only; that file
  is gitignored. Never commit it without redaction.

## How it isolates from your existing setup

Uses `claude --bare --settings runtime/_empirical/harness/settings.json`.

`--bare` disables user-level hooks (crystl, roux, agentic-stack global hooks)
and auto-memory. `--settings` overlays *only* the harness hooks. So this run
does not interact with your normal Claude Code configuration in any way.

## Running

```bash
# Fire 10 mixed sessions (default)
bash runtime/_empirical/harness/run_synthetic_sessions.sh

# Fire 50 mixed sessions for a real telemetry pass
bash runtime/_empirical/harness/run_synthetic_sessions.sh 50 mixed

# Long-session profile (more likely to provoke compaction)
bash runtime/_empirical/harness/run_synthetic_sessions.sh 5 long

# Aggregate
python3 runtime/_empirical/harness/aggregator.py --expected expected_runs.json
```

## Manual scenarios (require human, not in run_synthetic_sessions.sh)

Some hook events only fire under interactive conditions the harness cannot
simulate. Run these manually with the same `--bare --settings` flags:

| Scenario | How |
|---|---|
| `PostCompact` | Start an interactive session, fill it with output until compaction triggers. |
| `Stop` after `/clear` | Run a session, type `/clear`, wait. |
| `SubagentStop` | Run a session that uses the `Agent` tool (`Task` or persona spawn). |
| `Notification` | Trigger a permission prompt or idle notification. |

For each, run with:
```bash
RUNTIME_HARNESS=$(pwd)/runtime/_empirical/harness \
RUNTIME_HARNESS_RUN_TAG=manual-<scenario> \
claude --bare --settings runtime/_empirical/harness/settings.json
```

Then aggregate as above.

## Pass condition (gates Phase 1)

`SessionStart`, `UserPromptSubmit`, `PostToolUse`, and `Stop` each at >=90%
deliverability across the synthetic sessions. If any fall short, the runtime
design needs to find a different mechanism (e.g., re-injection on next
`UserPromptSubmit` if `SessionStart` is unreliable). Halt to `HALT.md` rather
than designing on assumptions.

## Output

```
_data/                        gitignored
  events.jsonl                # one line per hook firing (metadata-only)
  payload-samples.jsonl       # full stdin payloads (potentially sensitive)
  expected_runs.json          # what each run was supposed to fire
  .write.lock                 # flock sentinel
```

Aggregated reports go in `runtime/_empirical/phase0-empirical.md` (committed,
under data policy).
