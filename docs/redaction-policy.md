# Redaction policy

Three layers of defense before anything reaches a remote git repo.

## Layer 1: redact.py (public-token regex)

Lives in the framework. Catches well-known public token formats:

| Pattern | Regex |
|---|---|
| AWS access key | `AKIA[0-9A-Z]{16}` |
| GitHub PAT | `ghp_[A-Za-z0-9]{36}` |
| GitHub OAuth | `gho_[A-Za-z0-9]{36}` |
| GitHub server token | `ghs_[A-Za-z0-9]{36}` |
| GitHub user/app token | `ghu_[A-Za-z0-9]{36}` |
| GitHub refresh | `ghr_[A-Za-z0-9]{36}` |
| JWT-shaped | `eyJ...\.eyJ...\.[10+chars]` |
| Generic assignment | `(api_key\|secret\|password\|token)\s*[:=]\s*[30+chars]` |

False-positive suppression: any line containing `# redact-allow:` (or
the line immediately after) is skipped. Use this for test fixtures with
intentionally fake-looking values.

## Layer 2: redact_private.py / redact-private.txt (org-aware)

Created by the installer at `~/.agent/redact-private.txt`. Empty stub
by default. The user fills in patterns that matter for their org but
shouldn't be in the public framework's regex (e.g., `*.example.org`,
internal API key prefixes, Datadog tokens).

```
# Add one regex per line.
# Example:
# (?i)acme[_-]?api[_-]?key\s*[:=]\s*[A-Za-z0-9_-]{20,}
# (?i)dd_[a-z0-9]{32}
```

This file lives in the user's brain and gets committed to the private
brain repo. It's never in the public framework.

## Layer 3: trufflehog (entropy + pattern, optional)

If `trufflehog` is installed, `tools/sync.sh` invokes:

```bash
trufflehog filesystem ~/.agent/ --no-update --fail
```

before each push. Trufflehog catches high-entropy strings the regex
might miss (random-looking 32+ char values that don't match a known
prefix).

Install:
```bash
brew install trufflesecurity/trufflehog/trufflehog
```

## What none of these catch

- Custom org token shapes that don't match either regex layer or
  trufflehog's heuristics.
- Secrets in image data, PDFs, or other binary files (binary files
  are skipped by design — too noisy for regex).
- Memory entries that are themselves sensitive *content* (e.g.,
  internal incident details, customer data) — these aren't "secrets"
  in the credential sense but are still personal. Don't put them in
  the brain at all if you don't trust your private remote.

## What to do on a hit

The pre-commit hook prints:
```
~/.agent/memory/episodic/AGENT_LEARNINGS.jsonl:42:aws_access_key: AKIAIOSF...
```

Then exits 1, blocking the commit. Options:

1. **It's a real secret** — remove it from the brain. Edit the file,
   then re-attempt the commit. If the secret was already pushed in a
   previous commit, rotate the secret immediately and run
   `git filter-repo --replace-text` to scrub history.

2. **It's a false positive** — add a `# redact-allow:` marker on the
   line before. Re-run the commit.

3. **It's a recurring noise pattern** — add the false positive to
   `redact-private.txt` as a deny-pattern? No: the file is for *more*
   patterns to redact, not exemptions. For exemptions, the marker is
   the only mechanism. This is intentional — exempting a pattern
   globally is dangerous.

## Privacy audit before publishing the framework

The PUBLIC framework repo (`mustafa-agentic-stack`) must never contain
personal data. Before any push to that repo, run the audit checklist:

```bash
gitleaks detect --source ~/Documents/codebase/mustafa-agentic-stack/ --redact
trufflehog filesystem ~/Documents/codebase/mustafa-agentic-stack/
trufflehog git file://~/Documents/codebase/mustafa-agentic-stack/
git -C ~/Documents/codebase/mustafa-agentic-stack grep -nIi -E "<your-org>|<your-name>"
```

The greps should find only the repo's own self-references (README install
URLs, etc.). Any other hit is a leak — investigate before push.

See `PRIVACY_AUDIT_v0.1.0.md` for the per-release checklist.
