# Memory model

Four layers, borrowed from cognitive architecture. Each holds a different
flavor of state and gets written / read by different parts of the system.

| Layer | Path | Source | Lifecycle |
|---|---|---|---|
| **Working** | `~/.agent/memory/working/` | host-agent during a session | ephemeral; archived by `archive.py` |
| **Episodic** | `~/.agent/memory/episodic/AGENT_LEARNINGS.jsonl` | PostToolUse hook | append-only; decayed by `decay.py` |
| **Semantic** | `~/.agent/memory/semantic/` | `graduate.py` | permanent (manual prune only) |
| **Personal** | `~/.agent/memory/personal/` | `migrate.py` + manual edits | permanent |

## Working memory

`working/REVIEW_QUEUE.md` and `working/WORKSPACE.md` (transient files
the dream cycle and review tools update). Anything in `working/` is
short-lived — `archive.py` moves stale entries elsewhere on each
dream cycle.

## Episodic memory

Append-only JSONL. One row per tool call from Claude Code:

```json
{
  "reflection": "Edit handler.ts for auth bug",
  "tool": "Edit",
  "salience": 5,
  "timestamp": "2026-04-26T10:00:00",
  "tags": ["edit", "auth"]
}
```

The PostToolUse hook scores each call's importance (salience) by
domain (deploy/migrate/schema = 8, edit = 5, read = 2, etc.) — see
`harness/hooks/claude_code_post_tool.py` for the full pattern table.

`decay.py` drops entries older than the retention window on each dream
cycle to keep the file from growing unbounded.

## Semantic memory

Graduated lessons. `lessons.jsonl` is the source of truth; `LESSONS.md`
is a derived view rendered by `render_lessons.py`. Schema:

```json
{
  "id": "lesson_abc123",
  "claim": "Always run the exact CI test command, not just npm test",
  "conditions": ["ci", "test"],
  "evidence_ids": ["2026-04-19T22:09:32"],
  "status": "accepted",
  "accepted_at": "2026-04-19T22:09:32",
  "reviewer": "host-agent",
  "rationale": "PR #39 hid failures via npm test breadth",
  "cluster_size": 3,
  "canonical_salience": 7.5,
  "confidence": 0.605,
  "support_count": 0,
  "contradiction_count": 0,
  "supersedes": null,
  "source_candidate": "abc123",
  "why": "PR #39 passed npm test locally but CI failed because CI runs jest --selectProjects unit",
  "how_to_apply": "In Phase 0 of the agent team workflow, read .circleci/config.yml to find the exact command"
}
```

The last three fields (`why`, `how_to_apply`, `original_markdown_path`)
are brainstack extensions to upstream's schema. Backward
compatible: when absent, rendering matches upstream exactly.

`semantic/lessons/<filename>.md` holds the long-form companion markdown
preserved verbatim from the source feedback file. Use it when you need
the original prose; the JSONL row is for retrieval.

## Personal memory

Three subdirs:

- `personal/profile/` — facts about the user themselves (`user_*.md`
  files migrate here). Loaded at session start.
- `personal/notes/` — project context, session reflections, miscellaneous
  notes. `project_*.md`, `cycle-*.md`, and untyped `*.md` files migrate here.
- `personal/references/` — external resource pointers. `reference_*.md`
  files migrate here.

`personal/PREFERENCES.md` (optional) — top-of-mind preferences for how
the agent should work with the user. The first file the agent reads at
session start, when it exists.

## MEMORY.md

A one-line-per-entry index pointing at the layered files above. Loaded
into the agent's session context at startup. `migrate.py` rewrites this
on every run (idempotent); manual edits get overwritten — the migration
is the source of truth.

Format:

```
- [Title](semantic/lessons/some-lesson.md)
- [Title](personal/profile/user_x.md)
- [Title](personal/references/some-tool.md)
```

The relative paths resolve from `~/.agent/memory/`.
