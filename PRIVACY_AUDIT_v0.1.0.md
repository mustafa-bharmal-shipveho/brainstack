# Privacy audit — v0.1.0

**Date**: 2026-04-26
**Auditor**: Mustafa Bharmal (self-audit)
**Repo state at audit**: commit `<filled-after-tag>`

## Methodology

Three layers of scanning:

1. **Manual `git grep`** for personal terms (`mustafa`, `veho`, `shipveho`)
   and known secret patterns (`AKIA`, `ghp_`, `gho_`).
2. **`gitleaks`** entropy + pattern scan (if installed).
3. **`trufflehog`** filesystem + git-history scan (if installed).
4. **Visual review** of every file in the repo.

## Tooling availability at audit time

- `gitleaks`: NOT INSTALLED — flagged for follow-up before ANY public push
- `trufflehog`: NOT INSTALLED — flagged for follow-up before ANY public push
- `git grep`: ✓ used as primary heuristic

**Decision**: gitleaks + trufflehog must be installed and run before
flipping the repo from PRIVATE to PUBLIC. v0.1.0 ships PRIVATE only.

## Manual `git grep` results

### `mustafa` mentions (all allowed self-references)

All hits are either:
- The project's own name `mustafa-agentic-stack` (self-naming)
- Setup paths like `~/Documents/codebase/mustafa-agentic-stack/`
- Schema description `[mustafa-agentic-stack extension]`
- Author attribution (`Mustafa Bharmal` in NOTICE)

No personal hits beyond self-naming. ✓

### `veho` / `shipveho` mentions (all allowed: org-aware patterns)

All hits are documentation explaining how users add ORG-SPECIFIC
redaction patterns to `redact-private.txt`. Example regex shown:
`(?i)veho[_-]?api[_-]?key\s*[:=]\s*[A-Za-z0-9_-]{20,}`

This is generic *example* text demonstrating how to add a redaction
rule. It's not a real Veho secret. The framework needs to teach users
HOW to redact org-specific patterns; pretending Veho doesn't exist
when the author works there would be artificial.

✓ Decision: keep. These are documentation, not personal data.

### Secret-pattern scans (no hits)

- `AKIA[0-9A-Z]{16}`: 0 hits in tracked files (only in
  `agent/tools/redact.py`'s pattern definition; that's the *regex*
  not a real key, and intentional)
- `ghp_[A-Za-z0-9]{36}`: 0 hits in tracked files (regex only)
- `gho_`, `ghs_`: 0 hits

✓ No secrets in tracked content.

## Files redacted before v0.1.0

| File | Original (commit history) | Redacted to | Reason |
|---|---|---|---|
| `README.md:52` | `bharmalmustafa89/veho-agent-brain` | `your private GitHub remote (you configure this)` | Identifies user's personal GitHub account |

After this redaction, no user-identifying URLs remain. Author
attribution in `NOTICE` is intentional (Apache 2.0 §4(d)) and
appropriate for a public OSS framework.

## Untracked / gitignored files

`DESIGN_NOTES.md` is gitignored (per `.gitignore`). Local-only;
contains internal design decisions. Verified with
`git check-ignore DESIGN_NOTES.md`.

## Decision

**v0.1.0 ships as PRIVATE.**

Public-facing release blocked on:

1. Install `gitleaks`, `trufflehog`, run full audit, archive results.
2. Fresh-account smoke install (clone + run `./install.sh` on a clean
   user account; verify `~/.agent/` materializes empty).
3. One trusted external user reviews the repo for leaks I might miss.

After all three: flip to PUBLIC, tag `v0.1.0`.

## Follow-up

- [ ] `brew install gitleaks trufflesecurity/trufflehog/trufflehog`
- [ ] Re-run audit with both tools, archive results in
      `PRIVACY_AUDIT_v0.1.0_full.md`
- [ ] Fresh-account smoke install
- [ ] External reviewer pass
- [ ] Flip repo to PUBLIC

## Sign-off

- **Self-review**: ✓ 2026-04-26 (this document)
- **Tool review (gitleaks + trufflehog)**: pending
- **Fresh-account install review**: pending
- **External reviewer**: pending
