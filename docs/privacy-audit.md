# Privacy Audit

Version: 0.6.x
Status: self-audit by the project author; independent external review pending.
Pinned to commit: COMMIT_HASH_AT_RELEASE (fill in at release).
Supersedes: [docs/history/PRIVACY_AUDIT_v0.1.0.md](history/PRIVACY_AUDIT_v0.1.0.md).

This document states, as honestly as the author can, what brainstack does with
your data: what stays local, what can leave your machine, what is captured, and
where the remaining risks are. Every claim here is meant to be verifiable
against the code at the pinned commit. If you find a claim that the code does
not support, that is a bug in this document; please report it.

## What leaves your machine

There are exactly three paths by which anything can leave your machine, and all
three are under your control:

1. **Git push to the brain remote you configure.** If, and only if, you set a
   remote (`./install.sh --brain-remote <url>` or by wiring it later), the
   hourly sync job pushes your brain to that remote. With no remote configured,
   nothing is ever pushed. The minimal install configures no remote.
2. **One-time embedding model download.** On the first index, the embedding
   backend downloads a model (about 210 MB) to `~/.cache/fastembed`. This is a
   download to your machine, not an upload; after it, retrieval runs offline.
3. **Optional query expansion (`--expand`, off by default).** When you pass
   `--expand` to `recall query`, the query text (never memory bodies) is sent
   through your own local `claude` or `codex` CLI, which then talks to that
   tool's provider. This is off by default; default queries make no such call.

Beyond these, brainstack makes no network calls of its own: no telemetry, no
analytics endpoints, no phone-home. The runtime event log is local JSONL.

## What is captured by default

The full install captures:

- Claude Code hook events (prompts and tool calls) via the runtime hooks.
- New agent sessions, via the background session scanner, rolled into the
  brain.
- Episodic events as local JSONL under `~/.agent/memory/episodic/`.
- Nightly digests summarizing sessions.

Mirrored sources added with `--add-source` are synced to your remote (if you
configured one) unless you add them to the brain's `.gitignore`.

The minimal install captures none of this automatically: it is the recall CLI
plus an index over whatever you put in the brain yourself.

## Redaction coverage and limits

Redaction runs on the write path of every adapter (Claude session, Claude misc,
Codex, Cursor) and at sync time. It applies:

- Built-in credential patterns (API keys, tokens, common secret shapes).
- High-entropy string detection.
- Your own patterns from `redact-private.txt` in the brain root.

Limits, stated plainly:

- The built-in patterns target credentials, not arbitrary PII. Names, addresses,
  customer identifiers, and confidential prose are not redacted unless you add
  patterns for them.
- Regex-based scrubbing misses novel shapes by construction.
- Redaction fails open: if a private pattern is malformed, it warns on stderr
  and continues with the remaining patterns rather than blocking the write. It
  reduces leakage; it does not guarantee its absence.

## Mitigations added in this version

- **Sanitized injection surfaces.** Recalled excerpts injected into agent
  prompts are passed through a sanitizer that neutralizes wrapper-escape
  sequences, wraps each excerpt in explicit fences, and prepends a one-line
  preamble marking the content as untrusted data, not instructions.
- **Review-gated durable memory.** `recall remember` stages a lesson for review
  by default (`needs_review`), and dream-cycle candidates require your review
  before they become durable. A hijacked agent cannot silently persist a
  permanent lesson.
- **Provenance.** Recalled documents carry a provenance label (source and
  reviewer when known) so you can see where a surfaced memory came from.
- **Write-path redaction across all adapters.** Your `redact-private.txt`
  patterns now apply on every adapter, not just the digest path.

## Known residual risks

These are real and unresolved; adopt with them in mind:

- Sanitization is structural, not semantic. It neutralizes escape sequences and
  frames content as untrusted, but it cannot make adversarial prose inside a
  recalled memory semantically inert. A future LLM session may still be
  influenced by inert-but-persuasive text.
- Provenance is self-reported by the writer, not cryptographically signed.
- Recall scoping is coarse. You can exclude whole sources from the every-prompt
  auto-recall injection (`auto_recall.exclude_sources` in the config), so a
  sensitive mirrored source stays searchable on demand but is never injected
  automatically. But there is no per-memory or per-directory (cwd) scoping yet:
  a lesson captured while working in one repository can still surface in
  sessions on unrelated repositories. Finer-grained scoping is on the roadmap;
  until then, keep genuinely sensitive material out of the brain.
- The brain is not encrypted at rest.
- The graduation reviewer identity is a label, not authenticated.

## Verification checklist

| Check | Status | Date |
|---|---|---|
| `trufflehog` run against the framework repo before publishing | passing | 2026-06-11 |
| Built-in + private redaction patterns reviewed | pending | |
| No network calls outside the three documented paths (code grep) | passing | 2026-06-11 |
| Sanitizer adversarial tests pass (`tests/recall/test_sanitize.py`) | passing | 2026-06-10 |
| Write-path redaction tests pass (`tests/test_redact_write_path.py`) | passing | 2026-06-10 |

Notes on the 2026-06-11 checks:

- **trufflehog** (`trufflehog filesystem .`, v3.95.5, 622 chunks): 0 verified, 11
  unverified secrets. All 11 are non-secrets: 9 are fake credentials in
  `tests/test_redact.py` (the fixtures the redaction tests assert against) and 2
  are documentation placeholders in `CHANGELOG.md` (`https://user:secret@host`
  and an `ABCDEFGH`-placeholder Slack webhook). `gitleaks` is not installed on
  the audit machine; trufflehog alone covered the scan. This scans the
  *framework* repo (the published artifact); a user's personal brain is a
  separate git repo whose pushes are gated by `sync.sh`'s secret scanner.
- **Network-call grep**: no `requests`/`httpx`/`urllib`/`aiohttp`/`socket`
  client imports or calls in `recall/`, `agent/`, or `runtime/`. The only
  external-process egress is `git` (in `agent/tools/sync.sh`), the `claude`/
  `codex` CLI for optional `--expand`, and the FastEmbed model download (inside
  the `qdrant-client[fastembed]` dependency). These are the three documented paths.
- **Redaction-pattern review** stays *pending*: it is a judgment review best
  done by a human security reviewer, not an automated pass.
