# The warm recall daemon

`recall serve` keeps one retriever resident: the embedder is loaded and the
embedded Qdrant store is already open. A query costs tens of milliseconds
instead of the several seconds a cold process pays before it does any work.

That matters most for the Claude Code auto-recall hook, which fires on
**every prompt** under a hard timeout. Without the daemon the hook blows its
budget and injects nothing, silently, on every prompt.

## Reranking is opt-in, and off by default

The cross-encoder is **not** loaded unless you pass `--rerank`. On the
labeled calibration set (2026-09-04) it produced no measurable ordering
gain, for 4-13x the latency: 0.47-1.4 s with the config default
(`jinaai/jina-reranker-v1-turbo-en` over 20 candidates) against a
60-130 ms retrieval-only warm path. The hook's client budget is 800 ms, so
turning it on by default would have traded every prompt's injection for an
improvement that did not show up.

If you want to measure it on your own brain:

```sh
recall serve --rerank                                  # calibrated defaults
recall serve --rerank --reranker-model BAAI/bge-reranker-base --rerank-n 20
```

With `--rerank` on, the daemon uses `Xenova/ms-marco-MiniLM-L-6-v2` over 10
candidates — p50 230-280 ms, and better scoring than the config default.
Those are *defaults*, not overrides: if you explicitly set
`ranking.reranker_model` or `ranking.rerank_n` in your config, the daemon
honours your values, and the two flags above beat both. `recall serve
--status` reports the `reranker_model` and `rerank_n` actually in force,
plus `warmup_ms`, so a health check can compare what is running against
what was calibrated.

The relevance **gate** (`auto_recall_min_rerank`) stays at 0.0. Reranking is
for ordering only for now.

## The one thing to know first

**While the daemon runs, it OWNS the index.** Embedded Qdrant takes an
exclusive per-process lock, so any other process that opens the store
directly gets:

```
embedded Qdrant index is busy at ~/.cache/recall/qdrant; another recall
process is using it.
A recall daemon is running at ~/.agent/runtime/recall.sock and owns this
store. `recall query` and `recall reindex` route through it automatically;
stop it with `recall serve --stop` if you need direct access.
```

`recall query`, `recall reindex`, the auto-recall hook and `recall-mcp` all
route through the socket automatically, so you normally never see this. It
shows up when something opens the store on its own path — `recall eval`, an
old MCP server, or `recall query --no-daemon`.

## Install

macOS (launchd):

```sh
./install.sh --setup-daemon      # render + load the LaunchAgent
./install.sh --remove-daemon     # unload, delete, stop a resident daemon
```

A full `./install.sh --yes` does this for you (Default 6). Opt out with
`--no-daemon`. `--minimal` never installs it.

The unit is `~/Library/LaunchAgents/com.brainstack.recall-daemon.plist`,
rendered from `templates/com.brainstack.recall-daemon.plist`. It runs
`<repo>/.venv/bin/python3 -m recall.cli serve` with `KeepAlive=true`, so a
crash is restarted within `ThrottleInterval` (10 s).

Linux has no unit yet. Run it under your own supervisor:

```sh
BRAIN_ROOT=~/.agent recall serve
```

## Running it by hand

```sh
recall serve                          # foreground, default socket, no rerank
recall serve --socket /tmp/my.sock    # explicit path
recall serve --rerank                 # opt into the cross-encoder
recall serve --rerank --rerank-n 20   # ... over more candidates
recall serve --refresh-interval-s 60  # freshness pass every minute (max 300)
recall serve --idle-timeout-s 600     # exit after 10 idle minutes (default: never)

recall serve --status                 # exit 0 running, 1 not running
recall serve --status --json
recall serve --stop
```

`--idle-timeout-s` defaults to 0 (never exit) because launchd's `KeepAlive`
would just restart it, paying the model load again for nothing.

Startup logs one line you can read in
`~/.agent/runtime/logs/recall-daemon.stdout.log`:

```
recall serve: ready in 601 ms (socket=/Users/you/.agent/runtime/recall.sock, rerank=off, warmup=104 ms, warm=ok, refresh_interval_s=300)
```

## Index freshness is the daemon's job

Nothing else refreshes the index any more. A file the dream cycle or the
hourly import mirror just wrote is not queryable until a pass picks it up,
and the daemon is what runs those passes: one immediately after startup,
then every `refresh_interval_s` (default 300 s).

A pass (`recall.index.refresh_index_chunked`) is deliberately shaped so it
does not stall live queries:

- the filesystem walk and the mtime comparison run **outside** the retrieval
  lock;
- changed docs are embedded and upserted in chunks of 4 **under** the lock,
  so a concurrent query waits at most one chunk (~150-300 ms on CPU);
- the stale-point delete takes the lock once at the end.

A doc counts as changed when it has no indexed point, its mtime differs, or
its recorded index mode differs — the same rule `upsert_documents` uses, so
nothing is re-embedded needlessly.

While a pass has work outstanding, `refresh_pending` is true and every query
answer carries `index_stale: true`. A **failed** pass sets
`last_refresh_ok=false` plus `last_refresh_error`, keeps `index_stale` true,
and is retried on the next interval. The daemon keeps serving throughout:
serving a known-stale brain loudly beats dying, and beats serving a stale
brain silently.

Passes are **mutually exclusive**. A `reindex` op that arrives while the
background loop is mid-pass does not start a second one — it waits for the
running pass and returns its counts. Two concurrent passes would embed the
whole brain twice, and the one that finished first would clear
`refresh_pending` while the other was still upserting, so `index_stale`
would read false in the middle of a refresh.

Adding a new source to `config.json` still needs a restart.

## Protocol

AF_UNIX, `SOCK_STREAM`, socket mode `0600` (owner only — any local user
could otherwise read your brain). The `bind()` runs under a `0o077` umask,
so the socket is never world-connectable even for the instant between
creation and `chmod`; the process umask is restored straight afterwards.
One NDJSON request per connection. Connections are handled on threads so a
burst of hook fires is all answered; retrieval itself is serialized under
one lock because embedded Qdrant is not thread-safe.

Limits: 64 KB per request line (inclusive), 20000 chars per prompt, 2 s to
receive a complete request line, and a bounded server-side queue wait (see
`busy` under Errors).

### Requests

```json
{"v":1,"op":"query","prompt":"atomic writes","k":5,"session_id":"","source":null,"type":null,"rerank":null,"budget_ms":800}
{"v":1,"op":"status"}
{"v":1,"op":"reindex"}
{"v":1,"op":"shutdown"}
```

`rerank: null` means "use the daemon's own setting". `budget_ms` is
optional: it tells the daemon how long the client itself will wait, so a
client with a long budget can queue longer than the default bound instead of
being answered `busy`. Unknown fields are ignored, so omitting it is safe —
and `recall.daemon_client` does omit it today, so every client currently
gets the default bound.

### Query response

```json
{
  "v": 1, "ok": true,
  "results": [ ... ],
  "query_ms": 74,
  "degraded": false,
  "reranked": false,
  "index_stale": false,
  "model": {"embedder": "BAAI/bge-base-en-v1.5", "reranker": "none"}
}
```

Each result carries exactly these keys:

| key | notes |
|---|---|
| `path`, `source`, `title` | document identity |
| `name`, `type`, `description` | frontmatter-derived, sanitized |
| `score` | RRF score (cheap pre-filter) |
| `rerank_score` | cross-encoder score, or `null` when nothing reranked (the default) |
| `provenance` | trust label |
| `frontmatter` | allowlisted keys only (see below) |
| `body` | first 2000 chars |
| `content_sha256` | sha256 of the **full** body, not the truncated one |

`content_sha256` covering the full body is load-bearing: the session dedup
store compares that hash to decide whether a doc changed since it was last
injected. Hashing the truncated body would make every edit past char 2000
invisible.

Frontmatter is attacker-influenceable — any ingested doc sets it — and ships
straight into a model context, so it is an allowlist:
`created_by, source, reviewed_by, provenance, created, date, created_at,
name, type, description, needs_review`.

### Status response

`pid`, `uptime_s`, `queries_served`, `socket`, `rerank`, `reranker_model`,
`rerank_n`, `warmup_ms`, `embedder`, `mode`, `collections`, `degraded`,
`version`, plus the freshness
block: `index_age_s` (seconds since the last **successful** pass, `null`
before the first), `last_refresh_ok`, `last_refresh_error`,
`last_refresh_ts`, `last_refresh_ms`, `last_refresh_changed`,
`last_refresh_deleted`, `refresh_pending`, `refresh_interval_s`.

### Errors

```json
{"v":1,"ok":false,"error":"bad_request","message":"unknown op 'frobnicate'"}
```

| `error` | when |
|---|---|
| `bad_request` | malformed JSON, `v` ≠ 1, unknown op, missing/oversized prompt, oversized request |
| `internal` | retrieval raised |
| `busy` | the retrieval lock was not free in time — see below |

`busy` says which wait was actually in the way, and the `message`
distinguishes them:

- **a refresh held the lock** for more than `QUERY_LOCK_TIMEOUT_S` (2 s).
  Retrying shortly genuinely helps: the chunk finishes and the index is
  fresher. This is reported only while the pass really holds the lock, never
  during its lock-free discovery phase — otherwise every slow query during a
  long walk would blame a refresh that was blocking nothing.
- **another query held it** past the queue bound (2 s by default, ~2x the
  hook's 800 ms budget; `RECALL_DAEMON_QUEUE_TIMEOUT_S`, or 2x the request's
  `budget_ms` up to 60 s). Retrieval is serialized on purpose, so queueing is
  normal — but finishing a query whose client has already given up just holds
  the lock against clients that are still waiting.
- **the client hung up** while queued. The request is dropped without
  running.

## Client failure reasons

`recall.daemon_client` is stdlib-only on purpose: it runs inside the hook on
every prompt, and importing `recall.core` or `qdrant_client` there would burn
hundreds of milliseconds before any work starts.

Every failure maps to a named reason, and the split is not cosmetic:

| reason | meaning | safe to fall back in-process? |
|---|---|---|
| `no_socket` | nothing at the path; daemon not installed | yes |
| `connection_refused` | socket file exists, nobody listening; daemon died | yes |
| `timeout` | daemon accepted but did not answer in budget | **no** |
| `protocol_error` | answer was not our protocol | **no** |
| `server_error` | daemon answered `ok:false` | **no** |

The last three mean the daemon is alive and holds the store lock, so an
in-process fallback would only block on fcntl until it timed out anyway. The
hook reports `unavailable` instead and records `x_daemon_error`.

## Environment

| var | effect |
|---|---|
| `RECALL_DAEMON_SOCKET` | override the socket path everywhere (daemon, CLI, hook, doctor) |
| `RECALL_NO_DAEMON=1` | CLI, MCP and hook skip the socket and go in-process |
| `RECALL_CLI_DAEMON_BUDGET_MS` | how long `recall query` waits on the daemon (default 60000) |
| `RECALL_DAEMON_QUEUE_TIMEOUT_S` | how long the daemon queues a request behind another query before answering `busy` (default 2) |
| `RECALL_DAEMON_BUSY_BACKOFF_S` | how long `recall serve` sleeps before exiting 1 when another daemon owns the socket (default 30) |
| `BRAIN_ROOT` | socket defaults to `$BRAIN_ROOT/runtime/recall.sock` |

Resolution order for the socket: `$RECALL_DAEMON_SOCKET` → the config
literal → `$BRAIN_ROOT/runtime/recall.sock` → `$BRAIN_HOME`'s parent →
`~/.agent/runtime/recall.sock`. It lives in `recall.config.daemon_socket_path`,
and every probe (`recall doctor`, `recall serve --status`, `recall-mcp`)
resolves it from there rather than importing `recall.daemon`, which would
drag in `qdrant_client` for a path join.

`RECALL_DAEMON_BUSY_BACKOFF_S` exists for launchd: with `KeepAlive`, a
daemon that exits 1 because a manual `recall serve` owns the socket is
respawned every `ThrottleInterval` (10 s), so it would repeat the same
refusal in `recall-daemon.stderr.log` six times a minute forever. The
message names the pid holding the socket.

## Troubleshooting

**`recall doctor` says "Daemon: not running"** — every hook fire is taking
the slow in-process path. Run `./install.sh --setup-daemon`.

**"index is busy"** — see the top of this page. Something opened the store
directly. `recall serve --status` tells you whether a daemon is the cause.

**A second `recall serve` exits 1 with "already running (pid N)"** — that is
the guard working. Two daemons on one socket would mean two owners of an
exclusively locked store.

**The socket file exists but nothing answers** — a hard kill left it behind.
The next `recall serve` probes it, gets no answer, and unlinks it before
binding. No manual cleanup needed.

**`index_age_s` keeps growing / `last_refresh_ok` is false** — read
`last_refresh_error` from `recall serve --status --json`. The daemon is
still answering, but from an index that is falling behind.

**Results have `rerank_score: null`** — expected: the daemon does not
rerank unless you pass `--rerank`. If you did pass it and scores are still
null, the cross-encoder failed to load; check `recall serve --status`
(`rerank`, `reranker_model`, `rerank_n`) and the stderr log.

**Logs**

```
~/.agent/runtime/logs/recall-daemon.stdout.log
~/.agent/runtime/logs/recall-daemon.stderr.log
```

Restart after an upgrade (`./install.sh --upgrade` does this for you):

```sh
launchctl kickstart -k gui/$(id -u)/com.brainstack.recall-daemon
```
