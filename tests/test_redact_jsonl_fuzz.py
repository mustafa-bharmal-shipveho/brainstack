"""Light fuzz tests for the JSONL scrubber.

Generates synthetic JSONL rows containing a mix of:
  - known secret shapes (AWS, GitHub, Slack, Stripe, etc.)
  - random alphanumeric tokens (some high-entropy, some low)
  - benign prose
  - URLs (with and without embedded credentials)
  - nested dicts and arrays
  - Unicode, escape sequences, near-binary content

Then verifies invariants:
  - Scrubber never crashes
  - Output is always valid JSONL (one JSON object per non-blank line)
  - All known secret tokens are replaced with [REDACTED:...]
  - Re-running the scrubber on its output is a no-op (idempotency)
"""
import json
import random
import string
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "agent" / "tools" / "redact_jsonl.py"

# Known secret-shape generators. Each returns a string that should be
# detected by SOME pattern (regex or entropy).
def _gen_aws():
    return "AKIA" + "".join(random.choices(string.ascii_uppercase + string.digits, k=16))


def _gen_ghp():
    return "ghp_" + "".join(random.choices(string.ascii_letters + string.digits, k=36))


def _gen_slack_xapp():
    return f"xapp-1-A1234567890-1234567890123-{''.join(random.choices(string.hexdigits.lower(), k=64))}"


def _gen_stripe_live():
    return "sk_live_" + "".join(random.choices(string.ascii_letters + string.digits, k=24))


def _gen_authorization_bearer():
    return "Authorization: Bearer ghp_" + "".join(random.choices(string.ascii_letters + string.digits, k=36))


def _gen_url_userinfo():
    secret = "".join(random.choices(string.ascii_letters + string.digits + "+_-", k=32))
    return f"https://oauth2:{secret}@host.example/repo.git"


_SECRET_GENS = [
    _gen_aws, _gen_ghp, _gen_slack_xapp, _gen_stripe_live,
    _gen_authorization_bearer, _gen_url_userinfo,
]


def _gen_benign():
    """Random prose that should NOT trigger any pattern."""
    options = [
        "The quick brown fox jumps over the lazy dog",
        "Building feature flags for the launch next quarter",
        "Code review feedback: rename helper for clarity",
        "Merged PR #42 after CI green",
        "Updated dependencies to address CVE-2024-12345",
    ]
    return random.choice(options)


def _gen_random_chunk(length: int, alphabet: str = string.ascii_letters + string.digits) -> str:
    return "".join(random.choices(alphabet, k=length))


def _generate_row(rng: random.Random, expect_secret: bool) -> tuple[dict, list[str]]:
    """Build one synthetic JSONL row. Return (obj, list_of_secret_substrings)."""
    secrets_in_row: list[str] = []
    obj = {
        "ts": "2026-04-27T03:14:15Z",
        "tool": rng.choice(["Bash", "Edit", "Write", "Read", "TodoWrite"]),
        "salience": rng.randint(1, 9),
    }
    if expect_secret:
        s = rng.choice(_SECRET_GENS)()
        secrets_in_row.append(s)
        # embed in different field shapes
        shape = rng.choice(["bash_cmd", "edit_string", "nested_input", "list_args", "key"])
        if shape == "bash_cmd":
            obj["cmd"] = f"some prefix {s} suffix"
        elif shape == "edit_string":
            obj["input"] = {"old_string": s, "new_string": "REPLACEMENT"}
        elif shape == "nested_input":
            obj["payload"] = {"data": {"deep": {"secret": s}}}
        elif shape == "list_args":
            obj["argv"] = ["--token", s, "--verbose"]
        elif shape == "key":
            obj[s] = "value-at-secret-key"
    else:
        obj["msg"] = _gen_benign()
        obj["filler"] = _gen_random_chunk(rng.randint(0, 60))
    return obj, secrets_in_row


def test_fuzz_scrubber_invariants(tmp_path):
    rng = random.Random(42)  # deterministic for reproducibility
    rows: list[dict] = []
    expected_secrets: list[str] = []
    for i in range(200):
        has_secret = rng.random() < 0.3
        obj, secrets = _generate_row(rng, has_secret)
        rows.append(obj)
        expected_secrets.extend(secrets)

    f = tmp_path / "fuzz.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    # Run the scrubber
    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(f)],
        capture_output=True, text=True, timeout=60,
    )
    # Don't crash
    assert r.returncode in (0, 1), f"unexpected rc={r.returncode}, stderr={r.stderr!r}"

    # Output is valid JSONL line-by-line
    after = f.read_text()
    parsed = []
    for line_no, line in enumerate(after.splitlines(), start=1):
        if not line.strip():
            continue
        parsed.append(json.loads(line))
    assert len(parsed) == len(rows), (
        f"row count changed: was {len(rows)}, now {len(parsed)}"
    )

    # All expected secret substrings have been removed
    for s in expected_secrets:
        assert s not in after, (
            f"secret survived scrubbing: {s!r}"
        )

    # Idempotency: a second run should be a no-op
    r2 = subprocess.run(
        [sys.executable, str(SCRIPT), str(f)],
        capture_output=True, text=True, timeout=60,
    )
    assert r2.returncode == 0, (
        f"second scrub should report no changes; rc={r2.returncode}, stdout={r2.stdout!r}"
    )


def test_fuzz_scrubber_with_unicode_and_escapes(tmp_path):
    """Strings with unicode, control chars, JSON escapes — scrubber must not
    crash and must not corrupt valid JSON."""
    f = tmp_path / "unicode.jsonl"
    rows = [
        {"msg": "héllo wörld 中文 🎉", "key": "AKIAIOSFODNN7EXAMPLE"},
        {"escape": "tab\there\nnewline\\backslash\"quote", "k": "ghp_" + "A" * 36},
        {"ctrl": "", "padding": "x" * 100},
    ]
    f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")

    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(f)],
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode in (0, 1)

    # Re-load and verify it's still valid JSON
    for line in f.read_text().splitlines():
        if line.strip():
            json.loads(line)


def test_fuzz_scrubber_very_long_lines(tmp_path):
    """Lines with 100KB+ payloads should still scrub correctly."""
    f = tmp_path / "long.jsonl"
    big = "filler" * 20000  # 120KB
    rows = [
        {"big": f"{big} AKIAIOSFODNN7EXAMPLE {big}"},
    ]
    f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(f)],
        capture_output=True, text=True, timeout=15,
    )
    assert r.returncode == 1
    assert "AKIAIOSFODNN7EXAMPLE" not in f.read_text()
