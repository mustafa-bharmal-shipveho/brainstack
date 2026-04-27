# Redaction policy

Four layers of defense before anything reaches a remote git repo.

## Layer 1: redact.py (token regex + entropy)

Lives in the framework, runs as the brain repo's pre-commit hook. Built-in
patterns catch:

| Vendor / shape | Regex |
|---|---|
| AWS long-term access key | `AKIA[0-9A-Z]{16}` |
| AWS STS session token | `ASIA[0-9A-Z]{16}` |
| AWS IAM role/user IDs | `(?:AGPA\|AIDA\|AROA\|ANPA\|ANVA\|ASCA)[0-9A-Z]{16}` |
| GitHub PAT (classic) | `ghp_[A-Za-z0-9]{36,}` |
| GitHub PAT (fine-grained) | `github_pat_[A-Za-z0-9_]{82,}` |
| GitHub OAuth / server / user / refresh | `gh[osur]_[A-Za-z0-9]{36,}` |
| OpenAI legacy / project | `sk-[A-Za-z0-9]{32,}` / `sk-proj-…` |
| Anthropic | `sk-ant-…` |
| Slack tokens | `xox[abprs]-…` |
| Slack incoming webhooks | `https://hooks.slack.com/services/…` |
| Stripe live / test / restricted | `sk_live_` / `sk_test_` / `rk_live_` / `pk_live_` |
| Sentry DSN | `sntrys_…` |
| Datadog API key (in assignment) | `(?i)dd_api_key=…` |
| Google API key | `AIza[0-9A-Za-z_-]{35}` |
| JWT (3-part) | `eyJ...\.eyJ...\.[…]` |
| Authorization headers | `Authorization: Bearer …` / `Basic …` |
| Generic assignment | `(api_key\|secret\|password\|token\|client_secret\|private_key\|encryption_key\|session_token\|refresh_token)\s*[:=]\s*[30+chars]` |
| PEM private key blocks | multi-line `-----BEGIN … PRIVATE KEY-----` |
| OpenSSH private key | multi-line `-----BEGIN OPENSSH PRIVATE KEY-----` |
| PGP private key | multi-line `-----BEGIN PGP PRIVATE KEY BLOCK-----` |

In addition, a **Shannon-entropy sweep** flags lines containing tokens of
length ≥ 32 with entropy ≥ 4.5 bits/char. URL-bearing lines (`://`) are
exempt from the entropy sweep — Notion, Drive, GitHub, S3 URLs all naturally
contain high-entropy IDs that aren't credentials. Disable the sweep with
`--no-entropy` if it's noisy; tune with `--entropy-threshold N`.

False-positive suppression: any line containing `# redact-allow:` (or
the line immediately after) is skipped. Use this for test fixtures with
intentionally fake-looking values.

## Layer 2: redact-private.txt (org-aware patterns)

Created by the installer at `~/.agent/redact-private.txt`. Empty stub
by default. As of v0.1.1 this file is **actually loaded** — each
non-blank, non-comment line is compiled as an additional regex and
merged into the pattern set. Invalid regexes log a warning to stderr
and are skipped. Patterns containing ReDoS-prone shapes
(`(...+...)+` etc.) are rejected at load time (the redactor never
crashes or hangs the pre-commit flow because of a malformed user
pattern).

A starter set of org-PII shapes ships at
[`templates/redact-private.example.txt`](../templates/redact-private.example.txt).
Copy it over to seed common patterns:

```bash
cp ~/Documents/codebase/mustafa-agentic-stack/templates/redact-private.example.txt \
    ~/.agent/redact-private.txt
# then edit ~/.agent/redact-private.txt
```

```
# Add one regex per line. Lines starting with # are ignored.
# Examples:
(?i)acme[_-]?api[_-]?key\s*[:=]\s*[A-Za-z0-9_-]{20,}
(?i)dd_[a-z0-9]{32}
internal-token-[a-z0-9]{20}
```

This file lives in the user's brain and gets committed to the private
brain repo. It's never in the public framework.

## Layer 3: redact_jsonl.py (sync-time JSONL scrubber)

Hooks capture raw Bash commands, Edit text, and tool output into the
episodic JSONL *before* redaction runs. By the time `sync.sh` reaches the
pre-commit scanner, the JSONL has already accumulated secrets that flowed
through tool calls. The new `tools/redact_jsonl.py` is invoked by `sync.sh`
*before* staging:

```bash
python3 ~/.agent/tools/redact_jsonl.py \
    ~/.agent/memory/episodic \
    ~/.agent/data-layer
```

It walks every string field (recursively into lists and nested objects),
replaces secret-shaped substrings with `[REDACTED:<pattern_name>]`, and
rewrites the file atomically (temp + fsync + os.replace). Idempotent —
running it twice on a clean file is a no-op.

The same pattern set as Layer 1, including `redact-private.txt`, applies.

## Layer 4: trufflehog / gitleaks (REQUIRED at sync time)

`sync.sh` now refuses to push if neither `trufflehog` nor `gitleaks` is on
PATH (set `SYNC_ALLOW_NO_SCANNER=1` to override, *not recommended*).
Whichever is found is run against the brain dir; any hit aborts the push:

```bash
trufflehog filesystem ~/.agent/ --no-update --fail
# OR
gitleaks detect --source ~/.agent/ --no-git --redact
```

Install one:
```bash
brew install trufflehog
# or
brew install gitleaks
```

## Layer 5 (server-side): GitHub Action

`templates/brain-secret-scan.yml` is a GitHub Action that re-runs
trufflehog + gitleaks against every push and pull request to the brain
repo. This catches `git commit --no-verify` bypasses — the user can skip
their local pre-commit hook, but the server-side scanner has no escape
hatch. Install by copying it into `<brain>/.github/workflows/secret-scan.yml`.

## What none of these catch

- Custom org token shapes that don't match either regex layer or trufflehog's
  / gitleaks's heuristics. Add to `redact-private.txt` once you encounter one.
- Secrets in image data, PDFs, or other binary files (binary files are
  skipped by design — too noisy for regex).
- Memory entries that are themselves sensitive *content* (e.g., internal
  incident details, customer data, colleague names) — these aren't
  "secrets" in the credential sense but are still personal. Don't put
  them in the brain at all if you don't trust your private remote.
  See finding C1 in SECURITY_REVIEW.md.

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
