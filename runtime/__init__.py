"""brainstack runtime: auditable, deterministic context-injection runtime.

This module owns the *injection layer* — what we push into the agent's context
window via hooks, CLAUDE.md, and re-injection. It does not (and cannot) own the
model's KV cache or accumulated conversation history. "Eviction" here means
"demotion-from-injection on the next turn."

Layout:
  runtime/core/             host-agnostic: manifest, budget, policy, replay
  runtime/adapters/         host-specific shims (e.g. claude_code/)
  runtime/_empirical/       Phase 0 research artifacts; not shipped at runtime
"""

__version__ = "0.2.0-dev"
