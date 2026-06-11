# Operational notes

This document keeps contributor/operator context that is too detailed for the
README but still matters when changing brainstack internals.

## Pending review visibility

The dream cycle writes candidate lessons to `~/.agent/memory/candidates/`.
Unreviewed candidates are intentionally not auto-graduated. Dogfood testing
showed that hidden pending work can pile up for days, so brainstack now renders
`~/.agent/PENDING_REVIEW.md` and exposes the count at session start.

Supported surfaces:

| Surface | Setup |
|---|---|
| Claude Code `@` import in `~/.claude/CLAUDE.md` | `./install.sh --setup-pending-hook` |
| Cursor rules | `./install.sh --setup-cursor-rules` |
| Shell wrappers for AI CLIs | `./install.sh --setup-shell-banner` |
| All of the above | `./install.sh --setup-pending-review-all` |

Dogfood note: a Claude Code `SessionStart` hook was tested as the first
implementation, but that hook path did not reliably inject context into fresh
sessions on the tested build. The `@` import path was chosen because it uses
Claude Code's normal session-load behavior.

## Human-gated review

`recall pending --review` hands off to an interactive TTY-only triage flow. The
tool refuses to run without a TTY so an assistant cannot graduate or reject
candidates unattended. This is a structural rule, not just a prompt instruction.

When touching this area, keep these guarantees:

- The pending summary should tell the user to run `recall pending --review`.
- Review decisions must require explicit user input.
- Generated pending summaries are local operational state and should not be
  synced to the private brain remote.

## Framework purity

Brainstack should not ship real personal, employer, internal-service, or
customer-specific strings in framework code, docs, schemas, or tests. Use
generic examples such as `<your-org>`, `example-corp`, `internal-service`, and
`reviewer-agent`.

Exception: the canonical repository URL
(`github.com/mustafa-bharmal-shipveho/brainstack`) appears in install
instructions and the doc-truth test by deliberate decision. All examples,
fixtures, and schemas still use placeholders.

Before release, run a targeted string audit for known local/company terms and
confirm any remaining hits are legal provenance in `NOTICE` / `UPSTREAM.md`,
the canonical-URL carve-out above, or explicitly intentional documentation.

## Session digests

Raw tool-call logs are too noisy for long-term recall. Session digests summarize
Claude/Codex sessions into searchable markdown with title, domain tags,
decisions, learned context, and files touched. These digests are what recall
should surface when a user asks "did I work on this before?"

Related operators:

```bash
./install.sh --setup-digests
BRAIN_ROOT=$HOME/.agent python3 ~/.agent/tools/digest_cli.py backfill
BRAIN_ROOT=$HOME/.agent python3 ~/.agent/tools/digest_cli.py provider list
```

Digest-derived features include profile rollups, theme clustering, and proactive
context candidates. Keep prompts/framework code domain-agnostic; tags should be
extracted from session content, not from a fixed company taxonomy.

## Auto-recall

Auto-recall is on by default in the full install since v0.6.0 (opt out with
`--no-auto-recall`; the `--minimal` install does not enable it) and is
currently implemented for Claude Code's `UserPromptSubmit` hook. It runs recall per user prompt, injects bounded results
for that turn, and records telemetry consumable by `recall stats`.

The operational tradeoff is latency versus context quality:

- Short prompts, slash commands, and bare acknowledgements should be skipped.
- Timeouts should fail open so chat is not blocked.
- Scores are retrieval similarity, not factual accuracy.
- Lower-score hits should be treated as context, not authority.

Other clients can use recall through CLI or MCP today. Per-prompt auto-injection
for another client should be implemented as a client-specific adapter rather
than by weakening the core recall contract.

## Runtime boundary

The runtime records and replays what brainstack injects. "Eviction" means an
item will not be re-injected by brainstack on later turns unless explicitly
added again. It does not mean brainstack can inspect or evict tokens from a
vendor model's private KV cache.

Keep this distinction explicit in docs and user-facing output.

## Provenance

Some memory-pipeline files are derived from `codejunkie99/agentic-stack` under
Apache 2.0. Keep attribution centralized in `NOTICE` and `UPSTREAM.md`, and do
not remove those files or their file lists when refactoring the README.
