#!/usr/bin/env python3
"""Pre-commit secret scanner.

Scans a directory tree for known token patterns (AWS keys, GitHub tokens, JWTs,
Slack/Stripe/Datadog/Sentry/OpenAI, Authorization headers, generic high-entropy
secrets in `key: value` shape, PEM blocks). Optionally loads org-specific
patterns from `<target>/redact-private.txt`.

Behavior:
  - Walks the target directory recursively.
  - Skips binary files (any file containing a NUL byte in the first 8KB).
  - Respects per-line allowlist marker `# redact-allow: <reason>` —
    if the marker appears on the same line OR on the line immediately
    before the match, the match is suppressed.
  - Loads extra regex patterns from `<target>/redact-private.txt` (one regex
    per line, `#` starts a comment). The file lives in the user's brain, not
    in the public framework — so org-specific shapes (your employer's hostnames, internal
    token prefixes) can be added without leaking the regex itself.
  - Whole-file pass detects PEM blocks (multi-line) and Authorization headers.
  - Optional Shannon-entropy sweep flags lines with high-entropy substrings
    of length >= 32. Disable with `--no-entropy`.
  - Exits 0 if no matches.
  - Exits 1 with output `<file>:<line>:<pattern_name>: <matched-text>` for
    each hit.

Intended use as a pre-commit hook:

    #!/usr/bin/env bash
    python3 ~/.agent/tools/redact.py ~/.agent/ || exit 1

Usage:
    redact.py [--no-entropy] [--entropy-threshold FLOAT] <target-dir>
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable

# ----- Built-in patterns -----
# Each entry: (name, compiled-regex). Names appear in the output.
# Patterns must be conservative — false positives on a brain repo full of
# markdown + JSONL break the pre-commit flow and train users to bypass with
# --no-verify, which we don't want.
BUILTIN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # ---- AWS ----
    # Long-term access keys
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # STS session token (short-term)
    ("aws_session_key", re.compile(r"\bASIA[0-9A-Z]{16}\b")),
    # IAM user-specific (less common)
    ("aws_iam_id", re.compile(r"\b(?:AGPA|AIDA|AROA|ANPA|ANVA|ASCA)[0-9A-Z]{16}\b")),
    # AWS secret-access-key shape (40 base64 chars). Only flag in obvious context;
    # otherwise too prone to FPs. Caught by generic_secret_assignment instead.

    # ---- GitHub ----
    ("github_pat_classic", re.compile(r"\bghp_[A-Za-z0-9]{36,}\b")),
    ("github_pat_finegrained", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{82,}\b")),
    ("github_oauth", re.compile(r"\bgho_[A-Za-z0-9]{36,}\b")),
    ("github_server", re.compile(r"\bghs_[A-Za-z0-9]{36,}\b")),
    ("github_user_app", re.compile(r"\bghu_[A-Za-z0-9]{36,}\b")),
    ("github_refresh", re.compile(r"\bghr_[A-Za-z0-9]{36,}\b")),

    # ---- OpenAI / Anthropic ----
    ("openai_legacy", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("openai_project", re.compile(r"\bsk-proj-[A-Za-z0-9_-]{20,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{40,}\b")),

    # ---- Twilio / SendGrid / Heroku / NPM ----
    ("twilio_account_sid", re.compile(r"\bAC[a-f0-9]{32}\b")),
    ("twilio_auth_token", re.compile(r"\bSK[a-f0-9]{32}\b")),
    ("sendgrid_key", re.compile(r"\bSG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43,}\b")),
    ("heroku_oauth", re.compile(
        r"(?i)\bheroku\b[^\n]{0,40}[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}\b"
    )),
    ("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("mailgun_key", re.compile(r"\bkey-[a-f0-9]{32}\b")),

    # ---- Slack ----
    # xoxa, xoxb, xoxc, xoxd, xoxe, xoxp, xoxr, xoxs, xapp (bot/user/refresh/etc.)
    ("slack_token", re.compile(r"\bxox[abcdeprs]-[A-Za-z0-9-]{10,}\b")),
    ("slack_app_token", re.compile(r"\bxapp-[0-9]+-[A-Z0-9]+-[0-9]+-[A-Za-z0-9]+\b")),
    ("slack_webhook", re.compile(
        r"https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+"
    )),
    # URL credentials in userinfo (https://user:secret@host) — covers the
    # class of bypass where embedding the secret in a URL makes the entropy
    # sweep skip the line. We extract the password after the colon so the
    # display doesn't expose it.
    ("url_userinfo", re.compile(
        r"://[A-Za-z0-9%._~+\-]+:([A-Za-z0-9%._~+\-]{8,})@"
    )),

    # ---- Stripe ----
    ("stripe_live", re.compile(r"\bsk_live_[A-Za-z0-9]{20,}\b")),
    ("stripe_test", re.compile(r"\bsk_test_[A-Za-z0-9]{20,}\b")),
    ("stripe_pub_live", re.compile(r"\bpk_live_[A-Za-z0-9]{20,}\b")),
    ("stripe_restricted", re.compile(r"\brk_live_[A-Za-z0-9]{20,}\b")),

    # ---- Datadog / Sentry ----
    ("sentry_dsn", re.compile(r"\bsntrys_[A-Za-z0-9_-]{32,}\b")),
    ("datadog_api", re.compile(r"(?i)\b(?:dd[_-]?api[_-]?key|datadog[_-]?api[_-]?key)\s*[:=]\s*['\"]?[a-f0-9]{32}['\"]?\b")),

    # ---- Google ----
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),

    # ---- JWT ----
    ("jwt_three_part", re.compile(
        r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"
    )),

    # ---- Authorization headers ----
    # Bearer tokens that are NOT obviously placeholders
    ("auth_bearer", re.compile(
        r"(?i)\bauthorization\s*:\s*bearer\s+([A-Za-z0-9_\-\.=]{20,})"
    )),
    ("auth_basic", re.compile(
        r"(?i)\bauthorization\s*:\s*basic\s+([A-Za-z0-9+/=]{20,})"
    )),

    # ---- Generic high-entropy in key=value form ----
    # 30+ chars to avoid false positives on git hashes (40 hex chars) and
    # snowflake IDs. We accept env-var names like MY_TOKEN, APP_API_KEY,
    # SERVICE_PASSWORD by matching ([A-Z][A-Z0-9_]*)?KEYWORD([A-Z0-9_]*)?
    # — `\b` would not fire between the underscore and `=` because both
    # are word chars in Python re.
    ("generic_secret_assignment", re.compile(
        r"""(?ix)
        (?:^|[^A-Za-z0-9_])             # word-start, but not _ (env-var prefix safe)
        ([A-Z][A-Z0-9_]*[_\-])?         # optional prefix: MY_, APP_, X_, etc.
        (api[_\-]?key|secret(?:[_\-]?key)?|password|passwd|token|auth[_\-]?token
          |access[_\-]?token|client[_\-]?secret|private[_\-]?key|encryption[_\-]?key
          |session[_\-]?token|refresh[_\-]?token)
        ([_\-][A-Z0-9_]*)?              # optional suffix: _V2, _PROD, etc.
        \s*[:=]\s*
        ['"]?
        ([A-Za-z0-9_+/=\-]{30,})
        ['"]?
        """,
    )),
]


# ----- Multi-line patterns (whole-file scan) -----
# Pattern definitions contain BEGIN/END markers as regex literals; the inline
# `# redact-allow` comments on each literal-bearing line keep the redactor
# from flagging its own source.
MULTILINE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("pem_private_key", re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY-----"  # redact-allow
        r"[\s\S]*?"
        r"-----END (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY-----"  # redact-allow
    )),
    ("pgp_private_key", re.compile(
        r"-----BEGIN PGP PRIVATE KEY BLOCK-----[\s\S]*?-----END PGP PRIVATE KEY BLOCK-----"  # redact-allow
    )),
    ("ssh_private_key", re.compile(
        r"-----BEGIN OPENSSH PRIVATE KEY-----[\s\S]*?-----END OPENSSH PRIVATE KEY-----"  # redact-allow
    )),
]


# ----- Allowlist marker -----
# Marker must appear as a true comment: preceded by whitespace or line-start.
# This rejects abuse where the marker is buried inside a JSON string field
# or quoted secret value to suppress detection on a neighboring line.
# `(?:^|\s)` enforces the comment-position constraint without requiring the
# whole line to be a comment (we still allow inline form like
# `KEY = "..." # redact-allow: fixture`).
ALLOWLIST_MARKER_RE = re.compile(r"(?:^|\s)#\s*redact-allow\b", re.IGNORECASE)


# ----- Filename-based skip rules -----
# These files often contain regexes/sample tokens for redaction itself.
# We still scan them, but tolerate false positives via the allowlist marker.
SKIP_DIRS = frozenset({".git", "__pycache__", ".pytest_cache", "node_modules", ".venv"})


def is_binary(path: Path) -> bool:
    """Return True if file appears binary (NUL byte in first 8KB)."""
    try:
        with path.open("rb") as f:
            chunk = f.read(8192)
        return b"\x00" in chunk
    except OSError:
        return True


def iter_files(root: Path, skip_files: set[Path] | None = None) -> Iterable[Path]:
    """Yield text files under root, skipping VCS/cache, binaries, and the
    explicit skip_files set. The skip_files set is real-path resolved before
    comparison.
    """
    skip_resolved: set[Path] = set()
    if skip_files:
        for f in skip_files:
            try:
                skip_resolved.add(f.resolve())
            except OSError:
                pass
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        try:
            if p.resolve() in skip_resolved:
                continue
        except OSError:
            pass
        if is_binary(p):
            continue
        yield p


# ReDoS-prone patterns: the constructs `(.+)+`, `(.*)+`, `(a+)+`,
# overlapping alternations followed by `+` etc. exhibit catastrophic
# backtracking on benign input. We refuse to compile any user pattern
# containing these shapes — better to print a clear error than to hang
# the pre-commit indefinitely and train users to bypass with --no-verify.
_REDOS_RE = re.compile(
    r"""
      \([^)]*[+*][^)]*\)[+*]    # (...+...)+ or (...*...)*  — nested quantifier
    """,
    re.VERBOSE,
)


def _is_redos_dangerous(pattern: str) -> bool:
    """True if the pattern likely exhibits catastrophic backtracking.

    Heuristic — not a complete safety check, but catches the classic
    nested-quantifier shape (`(...+...)+`) that shows up in CTFs and
    incident postmortems. Better to ship a tighter pattern than to lose
    the pre-commit hook to a 30-second hang.
    """
    return bool(_REDOS_RE.search(pattern))


def load_private_patterns(target_root: Path) -> list[tuple[str, re.Pattern[str]]]:
    """Load extra regex patterns from <target>/redact-private.txt.

    Each non-blank, non-comment line is compiled as a regex. Invalid regexes
    are reported on stderr and skipped (we never want a malformed user
    pattern to crash the pre-commit hook). Patterns matching ReDoS shapes
    are also rejected with a warning.
    """
    private_file = target_root / "redact-private.txt"
    if not private_file.exists():
        return []

    patterns: list[tuple[str, re.Pattern[str]]] = []
    try:
        lines = private_file.read_text().splitlines()
    except OSError as e:
        sys.stderr.write(f"redact: could not read {private_file}: {e}\n")
        return []

    for i, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if _is_redos_dangerous(line):
            sys.stderr.write(
                f"redact: ReDoS-prone pattern in {private_file}:{i} "
                f"(nested quantifier like `(...+)+`); rejected. Rewrite with "
                f"a non-backtracking shape.\n"
            )
            continue
        try:
            patterns.append((f"private_{i}", re.compile(line)))
        except re.error as e:
            sys.stderr.write(
                f"redact: invalid regex in {private_file}:{i}: {e} -- skipped\n"
            )
    return patterns


def shannon_entropy(s: str) -> float:
    """Shannon entropy in bits per character. Empty string returns 0."""
    if not s:
        return 0.0
    counts = Counter(s)
    length = len(s)
    return -sum(
        (n / length) * math.log2(n / length) for n in counts.values()
    )


# Tokens of length >= ENTROPY_MIN_LEN with entropy >= threshold are flagged.
# We deliberately exclude `/+=` from the token class so URLs and filesystem
# paths split into multiple shorter tokens that fall below the length floor.
# Real base64-ish secrets are caught by the prefix-aware patterns above.
ENTROPY_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{32,}")
ENTROPY_DEFAULT_THRESHOLD = 4.5  # bits/char; >=4.5 is empirically random-looking
# Strings that are obviously not secrets even though they're high-entropy.
ENTROPY_IGNORE = re.compile(
    r"^(?:[a-f0-9]{32,}|[0-9]+|[a-zA-Z]+)$"  # pure hex / pure digits / pure alpha
)


def scan_file(
    path: Path,
    extra_patterns: list[tuple[str, re.Pattern[str]]],
    entropy_threshold: float | None,
) -> list[tuple[int, str, str]]:
    """Return list of (line_number, pattern_name, matched_text) hits."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []

    lines = text.splitlines()

    # Allowlist line-set: the marker line and the line immediately following.
    allow_lines: set[int] = set()
    for i, line in enumerate(lines, start=1):
        if ALLOWLIST_MARKER_RE.search(line):
            allow_lines.add(i)
            allow_lines.add(i + 1)

    hits: list[tuple[int, str, str]] = []
    all_patterns = BUILTIN_PATTERNS + extra_patterns

    # Per-line scan. We report ALL matches per line (and from multiple
    # patterns) — a line containing two distinct secrets must surface both,
    # and a line containing two AWS keys must surface both. The previous
    # `break` after the first hit silently dropped the rest, which red-team
    # testing showed could be exploited (B12).
    for i, line in enumerate(lines, start=1):
        if i in allow_lines:
            continue
        # Track positions already hit on this line so two patterns matching
        # the same span don't double-report (e.g. `Authorization: Bearer ghp_…`
        # would otherwise match both auth_bearer and github_pat_classic).
        consumed_spans: list[tuple[int, int]] = []
        for name, pat in all_patterns:
            for m in pat.finditer(line):
                if any(s <= m.start() < e for s, e in consumed_spans):
                    continue
                matched = m.group(0)
                if name == "generic_secret_assignment" and m.lastindex and m.lastindex >= 4:
                    matched = m.group(4)
                elif name == "url_userinfo" and m.lastindex and m.lastindex >= 1:
                    matched = m.group(1)
                elif name in ("auth_bearer", "auth_basic") and m.lastindex and m.lastindex >= 1:
                    matched = m.group(1)
                hits.append((i, name, matched))
                consumed_spans.append((m.start(), m.end()))

    # Whole-file scan for multi-line patterns. Track interior lines so the
    # entropy sweep below doesn't double-flag the random-looking base64 body
    # of a PEM block (the block as a whole is already reported).
    multiline_interior: set[int] = set()
    for name, pat in MULTILINE_PATTERNS:
        for m in pat.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            end_line_no = text.count("\n", 0, m.end()) + 1
            if line_no in allow_lines:
                continue
            hits.append((line_no, name, m.group(0)[:40] + "..."))
            multiline_interior.update(range(line_no, end_line_no + 1))

    # Entropy sweep (optional)
    if entropy_threshold is not None:
        # Pre-compute hit lines to skip
        hit_lines = {h[0] for h in hits} | multiline_interior
        for i, line in enumerate(lines, start=1):
            if i in allow_lines or i in hit_lines:
                continue
            # URLs naturally contain high-entropy IDs (Notion page IDs, GitHub
            # commit hashes, S3 object keys, Google Drive file IDs). They are
            # not secrets — the URL leaks them by definition. Vendor-specific
            # secret URLs (Slack webhooks) have explicit patterns above.
            if "://" in line:
                continue
            for token in ENTROPY_TOKEN_RE.findall(line):
                if ENTROPY_IGNORE.match(token):
                    continue
                if shannon_entropy(token) >= entropy_threshold:
                    hits.append((i, "high_entropy", token))
                    break  # one per line

    return hits


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan a directory for secrets before committing."
    )
    parser.add_argument("target", help="Directory to scan")
    parser.add_argument(
        "--no-entropy",
        action="store_true",
        help="Disable high-entropy string detection",
    )
    parser.add_argument(
        "--entropy-threshold",
        type=float,
        default=ENTROPY_DEFAULT_THRESHOLD,
        help=f"Shannon entropy threshold (default {ENTROPY_DEFAULT_THRESHOLD})",
    )
    args = parser.parse_args()

    root = Path(args.target).resolve()
    if not root.exists():
        sys.stderr.write(f"redact: target not found: {root}\n")
        return 2

    extra_patterns = load_private_patterns(root)
    entropy_threshold = None if args.no_entropy else args.entropy_threshold

    # Skip the redact-private.txt file itself — its own pattern bodies would
    # otherwise match themselves on whole-tree scans, training users to
    # bypass the pre-commit hook with --no-verify.
    skip_files = {root / "redact-private.txt"}

    total_hits = 0
    for f in iter_files(root, skip_files=skip_files):
        hits = scan_file(f, extra_patterns, entropy_threshold)
        for line_no, pattern_name, matched in hits:
            display = matched[:8] + "..." if len(matched) > 12 else matched
            print(f"{f}:{line_no}:{pattern_name}: {display}")
            total_hits += 1

    if total_hits:
        sys.stderr.write(
            f"\nredact: {total_hits} potential secret(s) found. Commit blocked.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
