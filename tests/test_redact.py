"""Tests for tools/redact.py — pre-commit secret scanner.

The redactor must:
  - flag known public token formats (AWS, GitHub, JWT, generic high-entropy)
  - exit non-zero on any hit (so pre-commit aborts the commit)
  - print <file>:<line>:<pattern_name> for each hit
  - respect a per-line allowlist (`# redact-allow: <reason>`)
  - skip binary files
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
REDACT_SCRIPT = REPO_ROOT / "agent" / "tools" / "redact.py"


def run_redact(target_dir: Path, *extra_args: str) -> subprocess.CompletedProcess:
    """Invoke redact.py on a directory; return the completed process."""
    return subprocess.run(
        [sys.executable, str(REDACT_SCRIPT), *extra_args, str(target_dir)],
        capture_output=True,
        text=True,
    )


def test_aws_access_key_blocks(tmp_path):
    f = tmp_path / "leak.txt"
    f.write_text("AWS_KEY=AKIAIOSFODNN7EXAMPLE\n")
    result = run_redact(tmp_path)
    assert result.returncode != 0
    assert "leak.txt" in result.stdout
    assert "aws_access_key" in result.stdout.lower() or "akia" in result.stdout.lower()


def test_github_pat_blocks(tmp_path):
    f = tmp_path / "config.py"
    f.write_text('TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"\n')
    result = run_redact(tmp_path)
    assert result.returncode != 0
    assert "config.py" in result.stdout


def test_github_oauth_token_blocks(tmp_path):
    f = tmp_path / "auth.txt"
    f.write_text("gho_abcdefghijklmnopqrstuvwxyz0123456789\n")
    result = run_redact(tmp_path)
    assert result.returncode != 0


def test_jwt_blocks(tmp_path):
    f = tmp_path / "token.txt"
    # Three-part JWT-shaped token
    f.write_text(
        "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c\n"
    )
    result = run_redact(tmp_path)
    assert result.returncode != 0


def test_generic_secret_pattern_blocks(tmp_path):
    f = tmp_path / "config.yml"
    f.write_text('api_key: "abc123def456ghi789jkl0mnop123qrs456"\n')
    result = run_redact(tmp_path)
    assert result.returncode != 0


def test_clean_file_passes(tmp_path):
    f = tmp_path / "readme.md"
    f.write_text("# A normal readme\n\nNothing secret here.\n")
    result = run_redact(tmp_path)
    assert result.returncode == 0


def test_allowlist_suppresses_match(tmp_path):
    f = tmp_path / "fixture.py"
    # Test fixture intentionally containing a sample-shape token, with allowlist marker
    f.write_text(
        '# redact-allow: example value used in test fixture\n'
        'EXAMPLE_KEY = "AKIAIOSFODNN7EXAMPLE"\n'
    )
    result = run_redact(tmp_path)
    assert result.returncode == 0, f"Allowlist should have suppressed match. stdout: {result.stdout}"


def test_multiple_files_scanned(tmp_path):
    (tmp_path / "a.txt").write_text("AKIAIOSFODNN7EXAMPLE\n")
    (tmp_path / "b.txt").write_text("ghp_abcdefghijklmnopqrstuvwxyz0123456789\n")
    result = run_redact(tmp_path)
    assert result.returncode != 0
    assert "a.txt" in result.stdout
    assert "b.txt" in result.stdout


def test_binary_file_skipped(tmp_path):
    # Embed a fake AWS key in a binary file — should be skipped
    f = tmp_path / "blob.bin"
    f.write_bytes(b"\x00\x01\x02AKIAIOSFODNN7EXAMPLE\x00\x03")
    result = run_redact(tmp_path)
    assert result.returncode == 0, f"Binary files must be skipped. stdout: {result.stdout}"


def test_output_format_includes_line_number(tmp_path):
    f = tmp_path / "leak.txt"
    f.write_text(
        "line one\n"
        "line two\n"
        "AKIAIOSFODNN7EXAMPLE on line three\n"
    )
    result = run_redact(tmp_path, "--no-entropy")
    assert result.returncode != 0
    # Format: <file>:<line>:<pattern>
    assert ":3:" in result.stdout, f"Expected line number 3 in output. stdout: {result.stdout}"


# ----- New patterns (Fix #4) -----


def test_aws_session_token_blocks(tmp_path):
    f = tmp_path / "session.txt"
    f.write_text("ASIAIOSFODNN7EXAMPLE\n")
    result = run_redact(tmp_path, "--no-entropy")
    assert result.returncode != 0
    assert "aws_session_key" in result.stdout


def test_github_pat_finegrained_blocks(tmp_path):
    f = tmp_path / "tok.txt"
    # github_pat_ + 11 chars + _ + 70+ chars (fine-grained format)
    token = "github_pat_" + "A" * 22 + "_" + "B" * 60
    f.write_text(f"{token}\n")
    result = run_redact(tmp_path, "--no-entropy")
    assert result.returncode != 0
    assert "github_pat_finegrained" in result.stdout or "github_pat" in result.stdout


def test_openai_project_key_blocks(tmp_path):
    f = tmp_path / "openai.env"
    f.write_text("OPENAI_API_KEY=sk-proj-" + "A" * 48 + "\n")
    result = run_redact(tmp_path, "--no-entropy")
    assert result.returncode != 0


def test_anthropic_key_blocks(tmp_path):
    f = tmp_path / "anthropic.env"
    f.write_text("KEY=sk-ant-" + "A" * 95 + "\n")
    result = run_redact(tmp_path, "--no-entropy")
    assert result.returncode != 0


def test_slack_token_blocks(tmp_path):
    f = tmp_path / "slack.txt"
    f.write_text("xoxb-1234567890-1234567890-AbCdEfGhIjKlMnOpQrStUvWx\n")
    result = run_redact(tmp_path, "--no-entropy")
    assert result.returncode != 0
    assert "slack_token" in result.stdout


def test_slack_webhook_blocks(tmp_path):
    f = tmp_path / "slack.txt"
    f.write_text("https://hooks.slack.com/services/T01ABCDEFGH/B01ABCDEFGH/aBcDeFgHiJkLmNoPqRsTuVwX\n")
    result = run_redact(tmp_path, "--no-entropy")
    assert result.returncode != 0


def test_stripe_live_key_blocks(tmp_path):
    f = tmp_path / "stripe.env"
    f.write_text("STRIPE=sk_live_" + "A" * 40 + "\n")
    result = run_redact(tmp_path, "--no-entropy")
    assert result.returncode != 0


def test_google_api_key_blocks(tmp_path):
    f = tmp_path / "g.env"
    f.write_text("AIzaSyD" + "A" * 32 + "\n")
    result = run_redact(tmp_path, "--no-entropy")
    assert result.returncode != 0


def test_authorization_bearer_blocks(tmp_path):
    f = tmp_path / "headers.txt"
    f.write_text("Authorization: Bearer abc123def456ghi789jkl0mnop123qrs456\n")
    result = run_redact(tmp_path, "--no-entropy")
    assert result.returncode != 0


def test_authorization_basic_blocks(tmp_path):
    f = tmp_path / "headers.txt"
    f.write_text("Authorization: Basic dXNlcjpwYXNzd29yZHRoYXR3aWxsZ2V0Zmxhc2g=\n")
    result = run_redact(tmp_path, "--no-entropy")
    assert result.returncode != 0


def test_pem_private_key_blocks(tmp_path):
    f = tmp_path / "key.pem"
    f.write_text(
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA1234567890abcdef\n"
        "ZmFrZWtleWNvbnRlbnRzZmFrZWtleWNv\n"
        "-----END RSA PRIVATE KEY-----\n"
    )
    result = run_redact(tmp_path, "--no-entropy")
    assert result.returncode != 0
    assert "pem_private_key" in result.stdout


# ----- Private patterns (Fix #2) -----


def test_private_patterns_loaded(tmp_path):
    """redact-private.txt patterns should be merged in."""
    (tmp_path / "redact-private.txt").write_text(
        "# Comment line\n"
        "(?i)acme[_-]?api[_-]?key\\s*[:=]\\s*[A-Za-z0-9_-]{20,}\n"
    )
    f = tmp_path / "config.txt"
    f.write_text('ACME_API_KEY=abc123def456ghi789jklmnopqrstuv\n')
    result = run_redact(tmp_path, "--no-entropy")
    assert result.returncode != 0, f"Expected private pattern hit. stdout: {result.stdout}"
    assert "private_" in result.stdout


def test_private_patterns_invalid_regex_does_not_crash(tmp_path):
    """An invalid private regex must be skipped, not crash the run."""
    (tmp_path / "redact-private.txt").write_text("[unclosed-bracket\n")
    (tmp_path / "clean.txt").write_text("nothing here\n")
    result = run_redact(tmp_path, "--no-entropy")
    assert result.returncode == 0  # invalid pattern skipped, file is clean
    assert "invalid regex" in result.stderr.lower()


def test_private_patterns_blank_and_comment_lines_ignored(tmp_path):
    (tmp_path / "redact-private.txt").write_text(
        "\n"
        "# this is a comment\n"
        "\n"
        "internal-token-[a-z0-9]{20}\n"
    )
    (tmp_path / "leak.txt").write_text("internal-token-abcdefghijklmnopqrst\n")
    result = run_redact(tmp_path, "--no-entropy")
    assert result.returncode != 0


# ----- Entropy detection (Fix #4) -----


def test_entropy_flags_high_entropy_string(tmp_path):
    # 40 chars of high-entropy random-looking content
    f = tmp_path / "blob.txt"
    f.write_text("token=Xq7Pv2Lm9Bn4Kc8Rf3Hd6Ws1Tj0Ge5Zu9Ay2Mh\n")
    result = run_redact(tmp_path)
    # Either generic_secret_assignment OR high_entropy
    assert result.returncode != 0


def test_entropy_off_with_flag(tmp_path):
    f = tmp_path / "high.txt"
    # High-entropy but no key=value shape; with --no-entropy this should pass
    f.write_text("Xq7Pv2Lm9Bn4Kc8Rf3Hd6Ws1Tj0Ge5Zu9Ay2Mh\n")
    result = run_redact(tmp_path, "--no-entropy")
    assert result.returncode == 0, f"--no-entropy should let raw blob pass. stdout: {result.stdout}"


def test_entropy_does_not_flag_pure_hex_hash(tmp_path):
    """Git-style hashes (40 hex chars) are common and shouldn't trip entropy."""
    f = tmp_path / "hash.txt"
    f.write_text("commit deadbeefcafebabedeadbeefcafebabedeadbeef\n")
    result = run_redact(tmp_path)
    assert result.returncode == 0, f"Pure hex should pass. stdout: {result.stdout}"


def test_entropy_does_not_flag_long_words(tmp_path):
    f = tmp_path / "prose.txt"
    f.write_text("Antidisestablishmentarianismsupercalifragilisticexpialidocious\n")
    result = run_redact(tmp_path)
    assert result.returncode == 0, f"Pure alpha should pass. stdout: {result.stdout}"


# ----- Redteam regressions -----


def test_url_userinfo_credential_blocks(tmp_path):
    """https://user:secret@host/ form must not slip through (B1)."""
    f = tmp_path / "url.txt"
    f.write_text("git remote add origin https://oauth2:Tx9JpQk2vNm5Lr8Hd6Yw@host.example/repo.git\n")
    result = run_redact(tmp_path, "--no-entropy")
    assert result.returncode != 0, f"URL userinfo password slipped through: {result.stdout}"
    assert "url_userinfo" in result.stdout


def test_my_token_env_var_blocks(tmp_path):
    """MY_TOKEN= and similar PREFIX_KEYWORD_SUFFIX env names must not slip (B5).

    Word boundary fails between `_` and `=` because both are word chars,
    so the pattern was missing the most common environment-variable form.
    """
    f = tmp_path / "env.sh"
    f.write_text(
        "MY_TOKEN=abc123def456ghi789jklmnop123qrst\n"
        "APP_API_KEY=xyz987abc123def456ghi789jkl012mn\n"
        "X_PRIVATE_KEY=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
    )
    result = run_redact(tmp_path, "--no-entropy")
    assert result.returncode != 0, f"env-var prefix forms slipped through: {result.stdout}"
    # All three env vars should be flagged
    out = result.stdout
    assert "env.sh:1" in out
    assert "env.sh:2" in out
    assert "env.sh:3" in out


def test_slack_xapp_token_blocks(tmp_path):
    """Newer Slack token shapes (xapp) must be caught (B9)."""
    f = tmp_path / "slack.txt"
    f.write_text("xapp-1-A1234567890-1234567890123-abcdef0123456789abcdef0123456789\n")
    result = run_redact(tmp_path, "--no-entropy")
    assert result.returncode != 0


def test_slack_xoxc_token_blocks(tmp_path):
    """xoxc client tokens (B9)."""
    f = tmp_path / "slack.txt"
    f.write_text("xoxc-1234567890-1234567890-AbCdEfGhIjKlMnOpQrSt\n")
    result = run_redact(tmp_path, "--no-entropy")
    assert result.returncode != 0


def test_redos_pattern_in_private_file_is_rejected(tmp_path):
    """A ReDoS-prone pattern in redact-private.txt must be rejected with a
    warning (B4). We verify by running on a benign input — if the regex
    were compiled, the run would either succeed or hang; with rejection,
    it should succeed AND print the rejection warning to stderr.
    """
    (tmp_path / "redact-private.txt").write_text(
        "(.+)+token\n"  # classic ReDoS shape
    )
    f = tmp_path / "input.txt"
    # Adversarial input that would hang a (.+)+ pattern
    f.write_text("a" * 50 + "X\n")
    result = run_redact(tmp_path, "--no-entropy")
    assert "ReDoS" in result.stderr or "rejected" in result.stderr.lower(), (
        f"expected ReDoS rejection warning; stderr: {result.stderr}"
    )


def test_npm_token_blocks(tmp_path):
    f = tmp_path / "npmrc"
    f.write_text("//registry.npmjs.org/:_authToken=npm_" + "A" * 36 + "\n")
    result = run_redact(tmp_path, "--no-entropy")
    assert result.returncode != 0


def test_twilio_account_sid_blocks(tmp_path):
    f = tmp_path / "twilio.env"
    f.write_text("TWILIO_ACCOUNT_SID=AC0123456789abcdef0123456789abcdef\n")
    result = run_redact(tmp_path, "--no-entropy")
    assert result.returncode != 0


def test_allow_marker_buried_in_string_does_not_suppress(tmp_path):
    """An attacker can't bury `# redact-allow` mid-string to suppress the
    next line's secret (B3). Marker must look like a real comment.
    """
    f = tmp_path / "attack.json"
    # The marker is in the middle of a JSON string field, not at start-of-line
    # and not preceded by whitespace.
    f.write_text(
        'BAIT="prefix# redact-allow suffix"\n'
        'AKIAIOSFODNN7EXAMPLE\n'
    )
    result = run_redact(tmp_path, "--no-entropy")
    assert result.returncode != 0, (
        f"buried marker should not suppress AWS key on next line; stdout: {result.stdout}"
    )


def test_allow_marker_with_leading_whitespace_works(tmp_path):
    """The legitimate case still works: marker as a real comment."""
    f = tmp_path / "fixture.py"
    f.write_text(
        '    # redact-allow: test fixture\n'
        '    EXAMPLE = "AKIAIOSFODNN7EXAMPLE"\n'
    )
    result = run_redact(tmp_path, "--no-entropy")
    assert result.returncode == 0


def test_allow_marker_inline_comment_works(tmp_path):
    """Inline marker on the same line as the secret still works (preceded by space)."""
    f = tmp_path / "fixture.py"
    f.write_text(
        'EXAMPLE = "AKIAIOSFODNN7EXAMPLE"  # redact-allow: test fixture\n'
    )
    result = run_redact(tmp_path, "--no-entropy")
    assert result.returncode == 0


def test_two_secrets_on_one_line_both_reported(tmp_path):
    """A line with two distinct token shapes must surface BOTH (B12).

    The previous implementation broke after the first match per line,
    silently letting the second slip through.
    """
    f = tmp_path / "double.txt"
    f.write_text(
        "AKIAIOSFODNN7EXAMPLE ghp_abcdefghijklmnopqrstuvwxyz0123456789\n"
    )
    result = run_redact(tmp_path, "--no-entropy")
    assert result.returncode != 0
    # Both pattern names should appear in stdout
    assert "aws_access_key" in result.stdout
    assert "github_pat" in result.stdout


def test_sendgrid_key_blocks(tmp_path):
    f = tmp_path / "sg.env"
    f.write_text("SG.aBcDeFgHiJkLmNoPqRsTuV.aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789aBcDeFgHiJk\n")
    result = run_redact(tmp_path, "--no-entropy")
    assert result.returncode != 0


def test_high_entropy_token_next_to_url_caught(tmp_path):
    """A bare 40+ char high-entropy token alongside an unrelated URL on the
    same line must still be flagged. The entropy sweep strips URL spans
    before scanning instead of skipping the whole line (regression N1).
    """
    f = tmp_path / "note.md"
    # The URL itself is innocuous; the token after it is the leak.
    f.write_text(
        "see PR https://github.com/x/y/pull/123 — used token "
        "fSk3iLp7qrTuVwxYz0aBcDeFg1HiJklMnOpqrstuvwxyzAB\n"
    )
    result = run_redact(tmp_path)
    assert result.returncode != 0, (
        f"high-entropy token next to URL slipped redact.py; stdout: {result.stdout}"
    )


def test_url_alone_still_does_not_false_positive(tmp_path):
    """The URL strip should not flag the URL itself."""
    f = tmp_path / "note.md"
    f.write_text(
        "Notion link: https://www.notion.so/example/Knowledge-Base-Page-deadbeefcafebabedeadbeefcafebabedeadbeef\n"
    )
    result = run_redact(tmp_path)
    assert result.returncode == 0, (
        f"URL alone should pass; stdout: {result.stdout}"
    )
