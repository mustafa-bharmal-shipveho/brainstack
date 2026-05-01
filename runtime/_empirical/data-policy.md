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
codify the contract. The synthetic battery at
`tests/runtime/synthetic/test_leak_battery.py` parametrizes 8 fake-secret
patterns across 3 surfaces (event log via summary, event log via input
keys, manifest via path/reason) and asserts none of them leak under
default settings. Run on every commit.

## Known threats (v0 — accepted with documentation)

Documented here so the threat model is visible to anyone reviewing or
adopting the runtime.

### sha256 of raw output as a fingerprint

`OutputSummary.sha256` is a SHA-256 of the raw tool output. SHA-256 is
not invertible, but for known content (publicly-leaked secrets, breach-
db entries, well-known canary values) the hash is a *fingerprint* a
sufficiently motivated party could match against a corpus.

Threat: an event log gets exposed and an attacker correlates one or more
output hashes against a known-leaked-secrets database, confirming that
secret X passed through this Claude Code session.

Mitigation today: sha256 is computed only when the runtime actually
holds the output text at hook time. Reference-only manifests don't
include this field.

Mitigation roadmap: v0.x will support `[tool.recall.runtime] hash_salt`
that mixes a per-session value into the hash, making cross-session
fingerprint correlation infeasible. Default would remain plain sha256
for v0 to keep the contract simple; users in higher-threat environments
opt in.

### `source_path` PII

Item `source_path` is stored verbatim. A path like
`/Users/yourname/Projects/secret-project/notes.md` reveals (a) your
username (b) the project name (c) the directory structure. brainstack's
hourly git push to your private remote treats these as content; if the
remote is ever compromised, paths leak.

Mitigation: the runtime does not normalize or hash paths. Producing
layers (the adapter, the storage layer) choose the convention. If you
care, normalize paths to a relative-to-`~/.agent/` or hashed-prefix form
before they reach the manifest.

### Harness `$PWD` capture

`runtime/_empirical/harness/hooks/log_event.sh` captures `$PWD` into the
event row. This is research-time telemetry only; the production
`runtime/core/events.py` does NOT capture cwd. The harness data dir is
gitignored. If you opt to commit harness data manually, redact `cwd`
fields first.

### Extension key abuse

`x_*`-prefixed keys round-trip through `extensions`. A careless or
malicious caller can set `x_full_payload` and stuff raw content there.
The runtime preserves it on round-trip; it does not enforce content
policy on extension values.

Mitigation: by convention, do not set extension keys you wouldn't be
willing to commit. v0.x may add a configurable max-bytes-per-extension
guard.
