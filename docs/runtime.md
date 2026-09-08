# brainstack runtime

This document is the design + reference for the v0.2 context runtime. Read
[`README.md`](../README.md) for the pitch and quickstart; this is the
deeper layer.

## Why this layer exists

Every memory product today (claude-obsidian, mem0, Letta, Zep, Cognee,
NotebookLM, OpenAI/Anthropic native memory) is a *storage and retrieval*
system. They debate vector DB vs graph vs vault, embedding model choice,
chunk size, RAG strategy. All of that is the database layer.

The thing that actually decides whether the model knows what it needs is
the context window itself: which 200k tokens are loaded right now, in
what order, with what salience. Storage and retrieval are upstream of
that, but the budget, the eviction, the promotion/demotion, the
*measurement* of context-window state — nobody owns that layer. CLAUDE.md
is a hand-rolled file. Hooks dump strings. RAG retrieval is "top-k by
cosine, hope it fits." There is no runtime.

The OS analogy is exact: today's memory tools are *disks*. What the
runtime adds is the *pager* — explicit working set, eviction policy,
page faults you can audit, telemetry on which pages got referenced.

## What this is NOT

- Not a vault. Not a vector store. Not an agent framework. Not an
  observability tool. Not a UI.
- Does not own the model's KV cache, accumulated conversation history,
  or the actual on-the-wire prompt sent to Anthropic. Those are opaque
  to any tool.

## What it IS

A small Python layer that sits between brainstack's retrieval (`recall/`)
and Claude Code's hooks. It maintains a per-session manifest of what is
in the *injected* context (CLAUDE.md, hot cache, retrieved chunks,
scratchpad), enforces token budgets, runs an eviction policy, and writes
an append-only audit log that supports turn-by-turn replay.

"Injected context" means: every byte we deliberately push into the
agent's context via hooks, CLAUDE.md, or `UserPromptSubmit` re-injection.
"Eviction" means: this item will not be re-injected on subsequent turns
unless something explicitly re-adds it.

## Architecture

```
recall.cli  -- runtime sub-app -- runtime/adapters/claude_code/cli.py
                                        |
                                  uses  v
                                runtime/adapters/claude_code/hooks.py
                                        |
                                  appends events to disk
                                        v
                              ~/.agent/runtime/logs/events.log.jsonl
                                        |
                                  consumed lazily by
                                        v
                                runtime/core/replay.py -> Engine
                                        |
                                  produces
                                        v
                                Manifest snapshots in-memory (write-to-disk
                                deferred to v0.x; today the manifest is
                                reconstructed on demand from the event log)
```

`runtime/core/` has zero adapter or Claude-specific imports. Adding a
Cursor or Codex CLI adapter is a contained change.

| Module | Purpose |
|---|---|
| `runtime/core/manifest.py` | Schema v1.1, dump/load, byte-deterministic round-trip |
| `runtime/core/events.py` | Event log schema v1.1; append-only with full item snapshots |
| `runtime/core/tokens.py` | Pluggable `TokenCounter`; offline default (deterministic ~±15% accuracy) |
| `runtime/core/policy/` | `Policy` Protocol + LRU, recency-weighted, pinned-first defaults |
| `runtime/core/budget.py` | `Engine` state machine: events in, manifest out, evictions enforced |
| `runtime/core/replay.py` | Reads event log, plays through Engine, emits per-turn manifests + diffs |
| `runtime/core/locking.py` | `locked_append`, `locked_write` (sentinel-flock pattern) |
| `runtime/adapters/claude_code/hooks.py` | `handle_hook(event)` — Claude Code hook entrypoint |
| `runtime/adapters/claude_code/cli.py` | `recall runtime` subcommand group |
| `runtime/adapters/claude_code/installer.py` | Idempotent `install-hooks` |

## Configuration ([tool.recall.runtime])

`RuntimeConfig.load()` reads a `[tool.recall.runtime]` table, merged **per
key** across up to three file layers (first layer that sets a given key
wins that key — a project file that sets one key does not reset every
other key to the dataclass default):

1. `$RECALL_RUNTIME_CONFIG` — explicit path override, content trusted as-is.
2. `./pyproject.toml` — consulted only when it carries a **non-empty**
   `[tool.recall.runtime]` table. A project's own build `pyproject.toml`
   (no such table at all — like brainstack's own, deliberately) is skipped
   rather than shadowing the layer below it.
3. `RuntimeConfig.global_config_path()` — `$BRAIN_ROOT/runtime/pyproject.toml`,
   defaulting to `~/.agent/runtime/pyproject.toml` when `$BRAIN_ROOT` is
   unset. This is install.sh's dedicated home for the section and is
   consulted whenever the file exists, even if its table is currently empty.

Any key no layer sets — or that every layer set to a malformed value — falls
back to the dataclass default below. `[tool.recall.runtime.budget]` merges
the same way, per sub-key. `RuntimeConfig.load(config_path=...)` (an
explicit path, used by callers that already know exactly which file they
want) is a single-file read: no layering, no fallback to the global file.

Example (this is the shape install.sh writes to the global file, NOT
something brainstack's own `pyproject.toml` carries — see precedence rule
2 above):

```toml
[tool.recall.runtime]
log_dir = "~/.agent/runtime/logs"
capture_raw = false
enable_auto_recall = true
auto_recall_min_score = 0.30

[tool.recall.runtime.budget]
claude_md = 4000
hot = 2000
retrieved = 20000
scratchpad = 10000
```

### Keys

| key | type | default | notes |
|---|---|---|---|
| `log_dir` | path | `~/.agent/runtime/logs` | event log + manifest + dedup-store root |
| `capture_raw` | bool | `false` | opt-in raw-payload capture, see "Data policy" below |
| `enable_reinjection` | bool | `false` | `UserPromptSubmit` re-injection of evicted items |
| `reinjection_budget_tokens` | int | `1500` | token cap for a reinjection block |
| `enable_auto_recall` | bool | `false` | fire `recall query` on every substantive prompt and inject top-K |
| `auto_recall_k` | int | `5` | results requested per auto-recall fire |
| `auto_recall_budget_tokens` | int | `1500` | token cap for the injected auto-recall block |
| `auto_recall_timeout_ms` | int | **1500** | hard wall-clock bound on the whole auto-recall worker — see rationale below |
| `auto_recall_min_chars` | int | `8` | prompts shorter than this never trigger auto-recall |
| `auto_recall_min_score` | float | `0.0` | reject RRF results below this score before injecting (`0.0` = floor off) |
| `auto_recall_daemon_budget_ms` | int | `800` | socket connect+respond budget when routing through the warm recall daemon; effective budget is `min(this, auto_recall_timeout_ms)` |
| `auto_recall_daemon_socket` | str | `"$BRAIN_ROOT/runtime/recall.sock"` | daemon socket path literal, resolved by `recall.config.daemon_socket_path` (env override > this literal > `$BRAIN_ROOT/runtime` > `$BRAIN_HOME` parent > `~/.agent/runtime`) |
| `auto_recall_min_rerank` | float \| `None` | `None` | reject candidates below this cross-encoder score; `None` (unset, or `"none"`/`"null"`) = gate off. Cross-encoder scores are raw logits (mostly negative), so `0.0` cannot express a calibrated negative threshold — any float, including a negative one, enables the gate. Only enforceable when the daemon supplies rerank scores |
| `auto_recall_dedup` | bool | `true` | kill switch for the per-session dedup store (don't re-inject an unchanged doc already shown this session) |

`auto_recall_timeout_ms` default is **1500ms**, down from an earlier 3000ms:
a warm recall daemon answers in roughly 60–130ms plus rerank, so the hard
bound on the whole worker no longer needs to absorb a cold Qdrant/embedder
load on every fire. When the daemon is down, the in-process fallback (which
still pays that cold-start cost) will legitimately time out under 1500ms;
that is reported honestly as `x_outcome=timeout` rather than silently
widening the bound to hide it.

Env vars: `RECALL_RUNTIME_CONFIG` (layer 1 above), `RECALL_DAEMON_SOCKET`
(overrides `auto_recall_daemon_socket` resolution unconditionally, including
for the daemon and CLI, not just the hook).

## The Engine

The Engine is the runtime's state machine. It receives a stream of typed
events and maintains the current injection set. When a bucket exceeds its
cap, the Engine calls the configured `Policy.choose_evictions()` and
demotes the chosen items.

```python
from runtime.core.budget import Engine, AddItem, TurnAdvance, SessionStart
from runtime.core.policy.defaults.lru import LRUPolicy

eng = Engine(
    budgets={"hot": 2000, "retrieved": 20000, "scratchpad": 10000},
    policy=LRUPolicy(),
    session_id="my-session",
)
eng.apply(SessionStart(ts_ms=0))
eng.apply(AddItem(
    id="c-001", bucket="retrieved",
    source_path="recall/results/foo.md", sha256="...",
    token_count=412, retrieval_reason="post-tool-use:Read",
))
manifest = eng.snapshot()  # current state as a Manifest dataclass
```

The Engine is **pure**: same events in -> same manifest out. This is what
makes replay honest — running an Engine against a recorded log produces
byte-identical manifests to what the live session produced.

## The control property

> An item evicted at turn N does NOT appear in the manifest at turn N+1
> unless an explicit `AddItem(id)` arrives at turn N+1.

This is the difference between a runtime and a logger. A logger records
what happened; a runtime decides what happens next.

Pinned in `tests/runtime/test_budget.py::test_evicted_item_does_not_reappear_without_explicit_readd`.

## Eviction policies

Policies are pure functions. They receive the current snapshot + a
"please free N tokens from bucket B" request and return an ordered list
of item IDs to evict.

Three defaults ship:

- **LRUPolicy** — evicts items with the lowest `last_touched_turn`. Pinned
  items are skipped. Stable across runs (sorts by id as tiebreak).
- **RecencyWeightedPolicy** — evicts items with the lowest `score` field
  first. Use when the runtime upstream populates `score` from a relevance
  signal richer than recency.
- **PinnedFirstPolicy** — pinned items are sacred; never evicted, even
  if the budget cannot be met without them. Among unpinned items, LRU.

Custom policies are one Python file:

```python
# my_policy.py
from runtime.core.policy import EvictionRequest, filter_to_bucket

class MyPolicy:
    def choose_evictions(self, request: EvictionRequest) -> list[str]:
        candidates = [it for it in filter_to_bucket(request.items, request.bucket) if not it.pinned]
        # ... your logic here ...
        return [it.id for it in chosen]
```

Wire it via `Engine(..., policy=MyPolicy())`.

## Schemas

### Manifest v1.1

Top-level required fields: `schema_version`, `turn`, `ts_ms`,
`session_id`, `budget_total`, `budget_used`, `items`. Top-level `x_*`
fields preserved across round-trip; non-`x_*` unknown fields rejected.

Per-item required fields: `id`, `bucket`, `source_path`, `sha256`,
`token_count`, `retrieval_reason`, `last_touched_turn`, `pinned`. Optional:
`score`, `extensions`. Per-item `x_*` extensions also preserved.

### Event log v1.1

Per-record required: `schema_version`, `ts_ms`, `event`, `session_id`,
`turn`. Optional: `event_id` (auto-derived from natural key if absent),
`tool_name`, `tool_input_keys` (sorted, no values), `tool_output_summary`
(`{sha256, byte_len}` — sha256 empty by default for security),
`bucket`, `item_ids_added`, `item_ids_evicted`, `items_added` (full
`InjectionItemSnapshot` records).

`x_*` extensions on event records are bounded to 1 KiB per value to
prevent payload smuggling.

## Data policy

Default behavior:

- Manifest items: path + sha256 + token count + bucket + reason. No raw
  content.
- Event log: tool name, input KEY NAMES (sorted), output SUMMARY (sha256
  empty by default + byte_len). No raw input values, no raw output text.

Opt-in:

```toml
[tool.recall.runtime]
capture_raw = true
```

When set, raw payloads go to a separate file
`~/.agent/runtime/logs/raw-payloads.jsonl` which is git-ignored at the
brainstack level. The runtime never writes raw content into the
default-on artifacts.

See [`runtime/_empirical/data-policy.md`](../runtime/_empirical/data-policy.md)
for the threat model and known-but-accepted threats (sha256 fingerprint
risk, source_path PII, extension key abuse).

## Determinism guarantees

- **Same events in -> same manifest out** within a Python interpreter
  version. Tested in `tests/runtime/test_integration_live_replay.py::
  test_live_session_replays_to_byte_equal_final_manifest`.
- **Locale and timezone independent.** `LC_ALL`, `LANG`, `TZ` do not
  affect manifest bytes. Tested in
  `tests/runtime/synthetic/test_timestamp_independence.py`.
- **PYTHONHASHSEED-safe.** All field orderings are sorted at dump time.
- **Cross-Python best-effort.** Token counter relies on `re` Unicode
  classes which are tied to the interpreter's Unicode DB. Replay across
  Python 3.10 and 3.13 should match for ASCII content; multi-byte input
  may differ by ±1 token at most.

## Roadmap

| Item | Status |
|---|---|
| Compaction-survival contract (PostCompact-driven re-injection) | v0.x — needs telemetry from `runtime/_empirical/harness/` |
| Citation-feedback loop (parse responses for cited chunks, demote uncited) | v0.x |
| Cross-context contradiction detection (across hot+warm tiers) | v0.x |
| HMAC-keyed alternative to sha256 in OutputSummary | v0.x |
| Cursor and Codex CLI adapters | community |
| Filesystem-backend interface formalized for non-brainstack backends | v0.x |
| Boundary-first frontier scoring (which items are "near the edge"?) | v0.x |
| MCP read-only surface for `manifest` + `replay` | v0.x |

## Phase 0 empirical research

The runtime ships with a hook telemetry harness at
`runtime/_empirical/harness/`. Run it once on a real machine to measure
hook deliverability under realistic Claude Code conditions:

```bash
bash runtime/_empirical/harness/run_synthetic_sessions.sh 50 mixed
python3 runtime/_empirical/harness/aggregator.py --expected expected_runs.json
```

The harness is research-only; it is gitignored at `_data/` and never
runs in production.

## See also

- [`runtime/_empirical/data-policy.md`](../runtime/_empirical/data-policy.md) — threat model + opt-in raw capture
- [`runtime/_review_outputs/SUMMARY.md`](../runtime/_review_outputs/SUMMARY.md) — codex + persona review trail
- `tests/runtime/test_*.py` — the contracts in code
