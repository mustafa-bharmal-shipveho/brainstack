# Runtime data policy v0.2

The runtime writes two streams to disk:

- **Manifest snapshots** — `runtime/.../manifest/turn-<N>.json`
- **Event log** — `runtime/.../events.log.jsonl` (append-only)

Plus, optionally and only when explicitly enabled, raw payload samples for
debugging.

This document defines what each stream may and may not contain. Codex review
flagged this as a v0 must-have: brainstack git-syncs everything, and a
runtime that audit-by-default-leaks-secrets is fatal.

## Default behavior (no opt-in)

| Stream | Contains | Does NOT contain |
|---|---|---|
| Manifest | source path, sha256, token count, bucket, retrieval reason, last-touched turn, pinned flag | raw file content, raw tool output, raw user prompts, env vars, secrets |
| Event log | event type, ts_ms, session id, turn, tool name, top-level tool-input KEY NAMES, output summary (sha256 + byte length), bucket, item ids added/evicted | tool input VALUES, tool output text, raw payloads, env vars |

Concretely: if a `Bash` tool fires with `command="echo SECRET_KEY=sk_live_..."`,
the event log row contains `tool_input_keys=["command"]` and an
`OutputSummary` with the SHA-256 of stdout. The string `SECRET_KEY` and the
echoed value never appear in any default-on file.

## Opt-in: raw capture (`capture_raw = true`)

Users who want full debugging may set in `pyproject.toml`:

```toml
[tool.recall.runtime]
capture_raw = true
```

When set, the runtime writes a separate file
`runtime/.../raw-payloads.jsonl` containing full tool inputs and outputs.
That file:

- defaults to `~/.agent/runtime/logs/raw-payloads.jsonl`, outside any git
  working tree
- is NEVER tracked by git
- is excluded by brainstack's git-sync `.gitignore` patterns
- is rotated on size

Users who turn this on should know what's in it. We do not redact for them.

## File locations

```
~/.agent/runtime/                           outside git working tree by default
  logs/
    events.log.jsonl                        safe-to-share metadata
    manifest/
      turn-<N>.json                         per-turn snapshot
    raw-payloads.jsonl                      opt-in only
```

The Phase 0 harness uses `runtime/_empirical/harness/_data/` for telemetry,
gitignored at that path. Production runtime uses the path under `~/.agent/`.

## What this means for git sync

brainstack syncs `~/.agent/` to a private remote hourly. The runtime writes
inside `~/.agent/runtime/logs/`, so:

- Default-on files (events log, manifests) sync. They are reference-only;
  they do NOT contain raw content.
- `raw-payloads.jsonl` is git-ignored at the brainstack level so it does
  NOT sync even if `capture_raw=true`.

To opt all runtime data out of sync entirely, add to your brainstack
`~/.agent/.gitignore`:

```
runtime/logs/
```

## What's still your responsibility

The runtime cannot tell whether a file path itself is a secret. If your
manifest contains `source_path: "secrets/aws-prod.env"`, the path is
preserved verbatim and synced. Mask paths upstream if that matters.

The runtime cannot tell whether a tool input key name leaks structure.
`tool_input_keys=["AWS_ACCESS_KEY"]` is preserved verbatim. Don't put
secrets in key names.

## Compliance with codex review

This policy implements the codex review fix:

> Define a data policy: default manifests store references/hashes + counts
> (no raw tool output); raw capture opt-in; safe log path + sync/commit
> protections.

The leak-test in `tests/runtime/test_harness_concurrent_flock.py::
test_metadata_only_no_raw_content_leak` and the prototype in
`tests/runtime/test_events.py::test_event_no_raw_input_fields_in_dump`
codify the contract. Sub-phase 2c will generalize these into a synthetic
fixture (`leak_test.py`) that runs every commit.
