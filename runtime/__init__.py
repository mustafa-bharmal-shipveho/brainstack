"""brainstack runtime: auditable, deterministic context-injection runtime.

**Phase 1 status (this version): contracts + logging primitives only.** Phase 1
defines the manifest schema, the event log schema, eviction policies as pure
functions, and a deterministic offline token counter. It does NOT yet enforce
budgets, write manifests automatically, or wire into Claude Code. That work
lives in phases 3 and 4 of the v0.2 plan.

When complete, the runtime will own the *injection layer* — what is pushed into
the agent's context window via hooks, CLAUDE.md, and re-injection. It does not
(and cannot) own the model's KV cache or accumulated conversation history.
"Eviction" in this codebase means "demotion-from-injection on the next turn."

Layout:
  runtime/core/             host-agnostic: schemas + pure functions
                            (manifest, events, tokens, policy)
  runtime/adapters/         host-specific shims (e.g. claude_code/)
                            — empty in Phase 1
  runtime/_empirical/       Phase 0 research artifacts; not shipped at runtime
"""

__version__ = "0.2.0-dev"
