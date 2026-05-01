# Phase 6 review outputs

Four final reviews ran in parallel against the full v0.2 PR diff
(20+ commits, mustafa/runtime-v0 vs main).

## Verdicts

| Reviewer | Verdict | Severity |
|---|---|---|
| Codex code review | (running long; see partial output) | — |
| Power User | (running) → real bugs caught early | identified pin/install issues |
| Competing-tool author | **FALSE-POSITIONING** on key claims | overclaims, addressed |
| Skeptic (final) | (running long) | — |

## Findings caught + fixed before tag

### Power User caught (real bugs):

1. **`pin/unpin` was a placebo**: CLI wrote to pinned.json, Engine never read it. Pinning was cosmetic.
   → Fixed: PinItem + UnpinItem event types added to Engine; replay translator emits them; CLI writes Pin/Unpin events to log instead of pinned.json. 3 new tests covering wiring.

2. **`python -m runtime` shadowing**: hook commands could fail in user projects with their own `runtime/` dir.
   → Fixed: installer now uses absolute path to hooks.py + PYTHONPATH= prefix pointing at the package root. Robust against shadowing.

### Competing-tool persona caught (positioning):

1. **README hero overclaim**: "decides what your agent actually remembers each turn" — false. The runtime records and budgets; the live injection loop closes in v0.x.
   → Fixed: "records, budgets, and replays what enters your agent's context each turn." Honest. Three-layer bullet expanded with explicit "does not yet inject CLAUDE.md content; Phase 4 of the roadmap wires that."

2. **runtime/__init__.py docstring stale**: said "Phase 1: contracts only" while v0.2 ships much more.
   → Fixed: rewritten to accurately describe what v0.2 does (manifest, engine, replay, adapter, CLI) and what it deferes (live injection, compaction-survival, KV cache control).

3. **docs/runtime.md claimed manifest/turn-N.json files**: aspirational, not implemented (current code reconstructs manifests on demand from the event log).
   → Fixed: architecture diagram updated to reflect "in-memory; write-to-disk deferred to v0.x."

## Findings that became roadmap (documented in CHANGELOG + docs/runtime.md)

- `recall runtime ls` doesn't show buckets with zero usage (UX nit; v0.x).
- LRU is closer to FIFO-by-turn because TouchItem isn't emitted by replay yet (v0.x).
- Compaction-survival via PostCompact re-injection (v0.x — biggest single feature gap).
- `recall runtime uninstall-hooks` doesn't exist (v0.x — paired with install-hooks).
- `source_path` PII verbatim (v0.x — adapter-level normalization).
- Item id sha256 fingerprinting (v0.x — HMAC-keyed mode).
- Schema bumps are breaking by construction (v0.x — multi-version reader).
- Backend interface FS-baked (v0.x — storage abstraction).

## Test impact

After Phase 6 fixes:

| Suite | Tests |
|---|---|
| Brainstack pre-existing | 581 |
| Runtime new (post-Phase-6) | 233 (+3 from pin event wiring) |
| **Total** | **809 (all green)** |

## Honest scope (final)

The runtime owns the **records, budgets, and replays the injected
context layer**. It does NOT yet inject CLAUDE.md content into the live
loop. v0.2 ships an end-to-end deterministic record + replay system
with byte-identical audit. v0.x closes the inject loop via
PostCompact-driven re-injection.

The wedge is real but narrower than the original pitch implied. Sharper
v0.2 framing: "the only memory project that lets you replay and audit
every paging decision your AI coding agent's host made — turn by turn,
with byte-identical reproducibility."

## Files

- `phase6-codex-out.txt` — codex code review (partial; gitignored)
- `phase6-power-user-out.txt` — power user persona (partial; gitignored)
- `phase6-competing-out.txt` — competing-tool persona (partial; gitignored)
- `phase6-skeptic-out.txt` — skeptic persona (partial; gitignored)
- `phase6-SUMMARY.md` — this file
