# LLM provider plugins

The digest layer is provider-agnostic. To drive it with a new LLM CLI,
add one file in this directory and register it in `__init__.py`.

## What a provider must do

A provider is a thin wrapper around an LLM CLI the user already has set
up locally. **The framework's promise is no separate API key** — the user
pays via their existing subscription / login, not a new bill. If a
provider requires an API key, document it loudly in the auth-check
return value so users see why it's marked unavailable.

Each provider implements two methods on the `LLMProvider` ABC (`base.py`):

```python
class MyToolProvider(LLMProvider):
    name = "my-tool"
    default_model = "my-tool-fast"

    def is_available(self) -> tuple[bool, str]:
        # (True, "")  when ready
        # (False, "<one-line user-facing fix>")  otherwise
        ...

    def invoke(self, system, prompt, *, model=None, json_schema=None,
               max_budget_usd=0.10, timeout_s=60) -> LLMResult:
        ...
```

Then in `__init__.py`:

```python
from .my_tool import MyToolProvider
PROVIDERS["my-tool"] = MyToolProvider()
```

That's it. Once registered, the provider participates in auto-detection
(`resolve_provider()` walks `PROVIDERS` in registration order). Users
can pin it with `BRAIN_LLM_PROVIDER=my-tool` or in `~/.agent/config.toml`.

## Contract details

- **Retry on schema fail:** when `json_schema` is given and the response
  fails to parse OR doesn't satisfy `schema["required"]`, retry exactly
  once with a stricter "JSON only" directive. Different prompt body —
  a naive retry just gets the same bad answer.

- **Errors:** raise `LLMError` for non-zero CLI exit, subprocess timeout,
  output that survives the retry. The adapter catches per-session so
  one bad call doesn't break the backfill loop.

- **Throttling:** honor `DIGEST_RATE_SLEEP_S` env var (sleep that many
  seconds before each subprocess call). This is how users dial down
  rate-limit pressure during the initial full backfill.

- **Token accounting:** populate `LLMResult.tokens_in` / `tokens_out`
  from the CLI's output when available. If the CLI only reports a
  combined total, put it in `tokens_in` and leave `tokens_out=None` —
  the backfill summary handles missing fields gracefully.

- **Cost:** populate `cost_usd` when the CLI reports a per-call dollar
  estimate (e.g. `claude -p` includes `total_cost_usd` in its JSON
  envelope). Leave `None` for subscription-billed CLIs that don't
  surface a number — this is the canonical case.

## Worked example: the codex provider

See `codex.py`. It's about 200 lines:

- `is_available()` checks `shutil.which("codex")` + `~/.codex/auth.json`.
- `invoke()` builds `codex exec --skip-git-repo-check`, pipes the
  prompt into stdin, runs subprocess, extracts the assistant region
  from Codex's framing preamble + trailing token report, tries strict
  JSON parse, falls back to balanced-brace extraction, retries once
  with a stricter directive on schema failure.

The Claude provider in `claude_code.py` is similar shape; it uses
`claude -p`'s native `--json-schema` and `--output-format json`
envelope so JSON handling is cleaner.

## No org-specific logic

This is a framework. Providers must not encode org-specific behavior
(e.g. "always summarize internal-tool-X work this way"). All such
context should come from the user's runtime input or `~/.agent/config.toml`
overrides. The pre-merge `grep` in the test plan enforces this — if your
provider hardcodes a company name, the audit will catch it.
