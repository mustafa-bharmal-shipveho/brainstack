# Security policy

`brainstack` handles credential redaction, captures tool-call history,
and runs hooks per Claude Code tool call. A bug in any of those layers
can leak data or weaken your guardrails. Please report security issues
privately rather than in a public issue.

## Reporting a vulnerability

Use **GitHub Private Security Advisories** for this repo:

> Repository → Security tab → "Report a vulnerability" → Open draft advisory

GitHub's PSA system lets us discuss the issue privately, propose a
patch, and coordinate disclosure without exposing anything until a fix
is shipped.

If you don't have a GitHub account, open a regular issue saying *only*
"I'd like to report a security issue privately, please reach out" with
a way to contact you. Don't include details in the public issue.

## Response timeline

This is a personal/open-source project with no SLA, but I aim for:

- **Acknowledgement:** within 7 days
- **Initial assessment:** within 14 days (severity, repro confirmation)
- **Patch / mitigation guidance:** depends on severity:
  - Critical (RCE, credential leak in default config) — best-effort within 14 days
  - High (security guardrail bypass with non-trivial setup) — within 30 days
  - Medium / Low — next release window

If you have an exploit you intend to disclose publicly, please give at
least **30 days** between report and disclosure.

## Supported versions

| Version | Status |
|---|---|
| `main` (HEAD) | actively patched |
| Tagged `v0.1.x` | best-effort backports for high-severity issues |
| Unreleased forks | unsupported |

## What this project tries to defend against

The threat model documented in [`docs/redaction-policy.md`](docs/redaction-policy.md)
and the architecture notes:

- **Credential leakage** through captured tool-call history (Bash
  output, Edit text, etc.). Five-layer redaction:
  pre-commit `redact.py` + `redact-private.txt` + sync-time
  `redact_jsonl.py` + required `trufflehog`/`gitleaks` + server-side
  GitHub Action.
- **`BRAIN_ROOT` env-poisoning RCE.** The hook wrapper validates
  `BRAIN_ROOT` resolves under `$HOME` and contains the vendored hook
  script before exec-ing.
- **Torn writes / data loss under concurrency.** Atomic writes via
  temp+fsync+os.replace; sentinel-locked appends so concurrent
  writers + dream cycle don't lose rows.
- **ReDoS** in user-supplied redaction patterns. `redact.py` rejects
  patterns containing nested-quantifier shapes at load time.
- **`.agent-local-override` spoofing.** Every fire is logged to
  `<brain>/override.log` so silent disabling is auditable.

## What this project does NOT defend against

- **Compromised local machine.** If your laptop is compromised, the
  attacker has access to your brain on disk, your shell history, and
  more. The brain repo is not encrypted at rest.
- **Compromised brain remote.** The brain pushes to a private GitHub
  repo of your choice. Account compromise on that remote exposes the
  brain. Use 2FA / hardware keys / separate identity as appropriate.
- **Sensitive content beyond credentials.** Memory entries can contain
  internal incident details, customer data, or PII. The redaction
  layers target *credentials*, not arbitrary sensitive content. See
  finding C1 in the brain's audit notes for guidance on moving brains
  between accounts.
- **Sophisticated persistent threats.** No multi-key signing, no
  TPM-backed enclave, no formal verification. This is hardening for
  ordinary mistakes, not nation-state adversaries.

## Hall of fame

If you report a valid vulnerability and want public credit, you'll be
listed here after the fix ships. Send a PR with the addition you want
once disclosure is complete.
