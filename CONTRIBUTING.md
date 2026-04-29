# Contributing to brainstack

Thanks for your interest. This is a small, opinionated framework. PRs
welcome but read the Scope section first to avoid wasted effort.

## Scope: what this project IS and ISN'T

**Is:** a persistent, git-synced memory layer for AI coding agents.
Captures tool calls, distills patterns nightly, surfaces lessons in
future sessions.

**Isn't:**
- A general note-taking app
- A replacement for project documentation
- A way to bypass model context limits via RAG (it's *learned context*,
  not retrieved context)
- A team-shared knowledge base (the brain is per-user; lesson sharing
  is roadmapped for v0.4)

PRs that drift outside this scope will be closed politely. If you're
not sure, open an issue first to discuss.

## Setting up a dev environment

```bash
git clone https://github.com/mustafa-bharmal-shipveho/brainstack.git
cd brainstack
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
make test         # 152+ tests should pass
```

Python ≥ 3.10 is required for the install path; the test suite passes
on 3.9 too (with a few hook-precedence tests skipped) for portability.

## Running tests

```bash
make test                  # full suite, verbose
make test-quick            # short summary
make test-fuzz             # only the fuzz + race-stress tests
make test-both-pythons     # 3.9 + 3.13 in series
```

Stress tests for atomic writes and concurrent appends use multiprocess
spawn — they're slow on macOS (~5s) but should always pass.

## Smoke-testing changes against a sandbox brain

```bash
# Sandboxed install in /tmp (doesn't touch your real ~/.agent)
PYTHON_BIN=/opt/homebrew/bin/python3.13 \
    ./install.sh --brain-root /tmp/brain-sandbox/.agent

# Run the dream cycle once
python3.13 /tmp/brain-sandbox/.agent/tools/dream_runner.py \
    --brain-root /tmp/brain-sandbox/.agent

# Cleanup
rm -rf /tmp/brain-sandbox
```

## What makes a good PR

- **Scope:** one focused change. Refactors and feature work go in
  separate PRs.
- **Tests:** new behavior needs a test. Bug fixes need a regression
  test that fails before your fix and passes after.
- **Security:** anything touching `redact*.py`, `_episodic_io.py`,
  `agentic_post_tool_global.py`, `auto_dream.py`, or `_atomic.py`
  needs a stress / adversarial test, not just unit tests. The
  framework's history has examples (`tests/test_concurrent_appends.py`,
  `tests/test_redact_jsonl_fuzz.py`).
- **Docs:** if you change behavior the user sees, update the relevant
  file in `docs/`.
- **No PII:** the framework must not contain real names, employer
  identifiers, internal URLs, or other PII even in test fixtures.
  Use placeholders (`Acme`, `<your-org>`, `Alice`, etc.). The test
  suite will reject some shapes via `redact.py`.

## What makes a PR get closed quickly

- Adds a feature outside the project's scope (see above)
- Adds a dependency without a strong reason (this project is
  intentionally low-dependency)
- Modifies vendored upstream code (`agent/memory/auto_dream.py`,
  `agent/memory/cluster.py`, etc.) without coordination — these
  files have an explicit modification log in `UPSTREAM.md` and rebase
  process. Open an issue first.
- Removes existing security guardrails (redaction layers, BRAIN_ROOT
  validation, sentinel locking) without replacement
- Has no tests

## Commit style

Conventional-ish:
- `feat(area): short summary`
- `fix(area): short summary`
- `docs: ...`
- `chore: ...`
- `test: ...`

Body explains the *why*, not the *what* (the diff shows the what).
Real-world incident references > abstract justifications. See
`feedback_*.md` lessons in the project's brain for examples.

## Reporting bugs

Open an issue. Include:
- Your OS + Python version
- Output of `./install.sh --verify`
- Steps to reproduce
- Expected vs actual behavior

For security issues, see [`SECURITY.md`](SECURITY.md) — do NOT open a
public issue with reproduction details for security bugs.

## Code of Conduct

By participating you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

By contributing, you agree your contributions are licensed under
Apache 2.0 (the project's license). No CLA, no DCO sign-off required.
