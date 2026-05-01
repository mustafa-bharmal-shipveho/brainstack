"""brainstack runtime: auditable, deterministic context-injection runtime.

**v0.2 status: records, budgets, and replays the injected-context layer.**
What ships in v0.2:

  - Manifest + event log schemas (deterministic, versioned, byte-identical
    round-trip).
  - Engine state machine (`runtime.core.budget.Engine`) that maintains the
    current injection set, enforces token budgets per bucket, and runs a
    pluggable eviction policy.
  - Replay engine (`runtime.core.replay`) that reconstructs per-turn
    manifests from the event log; integration test proves byte-equal
    output to the live engine.
  - Three default policies: LRU, recency-weighted, pinned-first.
  - Claude Code adapter (`runtime.adapters.claude_code`) that records
    hook firings and item snapshots into the event log.
  - `recall runtime` CLI: ls, pin/unpin, evict, replay, budget,
    install-hooks.

What v0.2 does NOT do (deferred to v0.x roadmap):

  - Inject CLAUDE.md content into the live model context. The runtime
    records what Claude Code injected; it does not itself enforce
    re-injection on the next turn. Compaction-survival via PostCompact
    re-injection is the v0.x feature that closes this loop.
  - Mutate the model's KV cache or accumulated conversation history.
    Those are opaque to any tool. "Eviction" in this codebase means
    "demotion-from-injection on the next turn."

Layout:
  runtime/core/             host-agnostic: schemas + pure functions +
                            Engine + replay
  runtime/adapters/         host-specific shims (currently: claude_code/)
  runtime/_empirical/       Phase 0 research artifacts; not shipped at runtime
"""

__version__ = "0.2.0-dev"
