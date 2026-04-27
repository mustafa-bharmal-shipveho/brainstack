# Dream cycle

The dream cycle is the consolidation pass: episodic events → patterns →
candidate lessons → review → graduated lessons. It separates **mechanical
staging** (cron-safe, no LLM) from **review with rationale** (manual,
high-stakes).

## Stages

```
PostToolUse hook                      ┐
  → AGENT_LEARNINGS.jsonl             │
                                       │  (Continuous: every tool call)
                                       ┘

       ▼  03:00 nightly via launchd

auto_dream.py (mechanical, no LLM):
  1. flock on .brain.lock
  2. load AGENT_LEARNINGS.jsonl
  3. cluster.py → recurring patterns
  4. promote.py → write candidates to memory/candidates/
  5. validate.py → heuristic prefilter (length + duplicate)
  6. decay.py + archive.py → drop stale entries
  7. write working/REVIEW_QUEUE.md summary
                                       ┐
                                       │  (Slow: nightly cron)
                                       ┘

       ▼  manual /dream

list_candidates.py:
  shows the queue, sorted by priority
                                       ┐
                                       │  (Burst: when host agent reviews)
                                       ┘

       ▼  one decision per candidate

graduate.py <id> --rationale "..."     OR     reject.py <id> --reason "..."
       │                                                     │
       ▼                                                     ▼
semantic/lessons.jsonl                          decisions.jsonl + candidate
   ↓ render_lessons.py                          marked status='rejected'
semantic/LESSONS.md
```

## Why mechanical staging

The boring parts (clustering, prefilter, decay) shouldn't depend on an
LLM. Reasons:

1. **Cron-safe**: no API keys, no network. `auto_dream.py` runs
   unattended without risking spurious calls or rate limits.
2. **Deterministic**: same input → same output. Test fixtures stay
   stable across runs.
3. **Low signal cost**: clustering finds patterns; deciding whether
   they're *good* patterns is the hard part. Don't waste model
   capacity on the mechanical step.

## Why required rationale

`graduate.py` requires `--rationale`. `reject.py` requires `--reason`.
Without these, the protocol degrades: rubber-stamped accepts pollute
the lesson set, silent rejects lose context. Required rationale forces
the host agent to articulate the decision, which:

- Makes the decision auditable (you can read past rationales to
  understand why a lesson exists).
- Catches drift (if the rationale doesn't match the cluster's evidence,
  the rationale will give it away).
- Preserves history (rejected candidates retain their full decision
  log; recurring churn is visible).

## /dream command

`~/.claude/commands/dream.md` is the host-agent prompt for running a
review pass. The default flow:

1. List pending candidates: `python3 ~/.agent/tools/list_candidates.py`
2. For each, read the cluster context
3. Decide graduate / reject / reopen with required text
4. Verify the rendered `LESSONS.md`

## Concurrency

`flock` on `~/.agent/.brain.lock` is held by:

- `auto_dream.py` for the duration of its read-modify-write cycle on
  `AGENT_LEARNINGS.jsonl`
- `tools/sync.sh` for the duration of its `git add + commit + push`

Two reasons:

1. **No torn writes**: dream rewrites the JSONL non-atomically. A
   concurrent sync mid-rewrite would commit partial state.
2. **No double-counting**: a tool-call appender that lands between
   dream's load and rewrite would otherwise be silently truncated.
   `_episodic_io.py` takes the same lock for appends.

## Schema extension fields

`lessons.jsonl` rows can carry three optional extension fields beyond
upstream's schema:

| Field | What it captures | Source |
|---|---|---|
| `why` | The reason behind the rule | `**Why:**` block in feedback markdown |
| `how_to_apply` | When/where the rule kicks in | `**How to apply:**` block |
| `original_markdown_path` | Path to long-form companion | `migrate.py` writes a verbatim copy |

Patched `_bullet_for` in `render_lessons.py` emits these as italic
sub-lines when present. When absent, the output matches upstream
exactly. See [`UPSTREAM.md`](../UPSTREAM.md) for the modification details.
