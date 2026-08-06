#!/usr/bin/env python3
"""Secret-scan gate for sync.sh — quarantine the suspect file, sync the rest.

Why this exists
---------------
sync.sh used to run ``trufflehog --fail`` and ``exit 1`` on any hit. That
is fail-closed AND global: one unverified false positive anywhere in the
brain stops the *entire* brain from ever reaching the remote, silently,
forever. It happened — a bare git SHA in an episodic command log
(``?ref=d257a25f…``, which ``Circle`` pattern-matches as a CircleCI
token) blocked every hourly sync for a month. The user only found out by
asking. A month of memories sat unpushed with no surface saying so.

The policy is now per-file, not global:

    if we are not sure about a file, skip THAT FILE and push everything else.

So this module returns a *quarantine list* — repo-relative paths whose
contents look risky — and sync.sh unstages exactly those and commits the
rest. A quarantined file stays dirty, so the next run re-evaluates it: the
moment the scrubber cleans it (or the user does), it syncs on its own.

Verified vs unverified
----------------------
A *verified* finding (trufflehog confirmed the credential is live against
the vendor's API) always quarantines. No filter can wave it through.

An *unverified* finding is only a shape match, and several detectors have
shapes that collide with content this brain emits constantly:

  ``Circle``       40 hex chars — every git SHA-1
  ``ProtocolsIO``  64 hex chars — every SHA-256 (brainstack names claim
                   files by SHA-256, and logs ``sha256sum`` output)
  ``Dockerhub``    a UUID — every session/event id brainstack mints

A bare hash with no vendor prefix is a hash. Real credentials from the
major vendors carry structural prefixes (``sk-ant-``, ``xoxb-``, ``ghp_``,
``AKIA``) that never match these anchored patterns, so filtering them
costs no real coverage. Verification is what disambiguates a genuine
40-hex CircleCI token from a git SHA — and verified hits are never
filtered, so that case is still caught.

Failure policy: fail CLOSED on scanner trouble. If the scan cannot run or
its output cannot be parsed, exit 2 and let sync.sh decide (it refuses to
push). An unscannable brain is different from a scanned-and-clean one.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# Detectors whose *unverified* hits are shape-indistinguishable from the
# hashes and ids the brain writes on every single run. Passed to
# trufflehog as --exclude-detectors so it doesn't even spend verification
# budget on them. `Privacy` and `NpmToken` were already excluded upstream
# for the same reason (they pattern-match UUIDs).
NOISY_DETECTORS = ("Privacy", "NpmToken")

# Path setup so we can import sibling modules without packaging — same
# idiom as _redact_common.py.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# ONE allowlist AND one suppression function, shared with redact.py / the
# pre-commit hook. If the two gates disagreed about what counts as a false
# positive, suppressing a finding here would only get the file staged and
# then rejected by the hook — converting a per-file skip back into a total
# block.
from redact import (  # noqa: E402
    SCAN_ALLOWLIST_FILENAME as ALLOWLIST_FILENAME,
    is_false_positive,
    load_scan_allowlist as load_allowlist,
)


def _rel(path_str: str, brain_root: Path) -> str | None:
    """Normalize a scanner-reported path to brain-root-relative.

    Returns None for anything sync.sh could not act on, so the caller
    never reports "held back 1 file" for a path it did not actually hold
    back.
    """
    if not path_str:
        return None
    p = Path(path_str)
    if not p.is_absolute():
        p = brain_root / p
    try:
        rel = str(p.resolve().relative_to(brain_root.resolve()))
    except ValueError:
        # Outside the brain — nothing we could stage anyway.
        return None
    # Git's own object store is not stageable, so quarantining it is a
    # no-op that still inflates the count. Loose objects also carry
    # already-committed content, which is the server-side scan's job.
    # (A brain with no .trufflehog-exclude.txt reaches this path.)
    if rel == ".git" or rel.startswith(".git/"):
        return None
    return rel


def _run_trufflehog(brain_root: Path) -> list[dict]:
    args = [
        "trufflehog", "filesystem", ".",
        "--no-update", "--json",
        "--exclude-detectors", ",".join(NOISY_DETECTORS),
    ]
    exclude = brain_root / ".trufflehog-exclude.txt"
    if exclude.is_file():
        args += ["--exclude-paths", str(exclude)]
    proc = subprocess.run(
        args, cwd=str(brain_root), capture_output=True, text=True
    )
    # trufflehog exits 0 without --fail even when it finds things; a
    # non-zero exit here means the scan itself broke.
    if proc.returncode != 0:
        raise RuntimeError(
            f"trufflehog exited {proc.returncode}: {proc.stderr.strip()[:400]}"
        )
    findings = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        meta = (
            d.get("SourceMetadata", {}).get("Data", {}).get("Filesystem", {})
        )
        # Verified=false means BOTH "checked the vendor, invalid" and
        # "could not check". Only the former is a conclusive verdict, and
        # only a conclusive verdict may license shape-based suppression.
        # trufflehog emits a verification-error field only in the latter
        # case; accept either spelling across versions.
        verr = d.get("VerificationError") or d.get("verification_error")
        findings.append(
            {
                "detector": d.get("DetectorName", "?"),
                "verified": bool(d.get("Verified")),
                "conclusive": not verr,
                "raw": d.get("Raw", "") or "",
                "file": meta.get("file", ""),
                "line": meta.get("line"),
            }
        )
    return findings


def _run_gitleaks(brain_root: Path) -> list[dict]:
    proc = subprocess.run(
        ["gitleaks", "detect", "--source", ".", "--no-git",
         "--report-format", "json", "--report-path", "-"],
        cwd=str(brain_root), capture_output=True, text=True,
    )
    # gitleaks exits 1 when it finds leaks; only >1 is a real failure.
    if proc.returncode > 1:
        raise RuntimeError(
            f"gitleaks exited {proc.returncode}: {proc.stderr.strip()[:400]}"
        )
    try:
        report = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"gitleaks report not JSON: {e}") from e
    return [
        {
            "detector": item.get("RuleID", "?"),
            # gitleaks has no verification step at all. That makes every
            # hit inconclusive, so shape-based suppression must stay off:
            # otherwise a real 40-hex CircleCI or legacy GitHub token
            # would be waved through as "just a git SHA".
            "verified": False,
            "conclusive": False,
            "raw": item.get("Secret", "") or "",
            "file": item.get("File", ""),
            "line": item.get("StartLine"),
        }
        for item in report
    ]


def _run_redact(brain_root: Path) -> list[dict]:
    """Findings from the brain's own redact.py — the pre-commit hook's engine.

    Included here so ONE quarantine decision covers both gates. Without
    this, sync.sh would unstage the trufflehog hits, commit, and then get
    the commit rejected by the pre-commit hook for a different file —
    back to an all-or-nothing block.

    Imported rather than shelled out because redact.py's CLI truncates
    each match to 8 characters, and the false-positive filters need the
    whole value to tell a Google Doc id from a credential.
    """
    try:
        from redact import (  # noqa: PLC0415
            ENTROPY_DEFAULT_THRESHOLD, iter_files, load_private_patterns,
            scan_file,
        )
    except ImportError as e:
        raise RuntimeError(f"cannot import redact.py: {e}") from e

    extra = load_private_patterns(brain_root)
    # redact.py's own main() skips these for the same reason: their
    # pattern bodies match themselves.
    skip = {
        brain_root / "redact-private.txt",
        brain_root / ALLOWLIST_FILENAME,
    }
    findings = []
    for f in iter_files(brain_root, skip_files=skip):
        for line_no, pattern_name, matched in scan_file(
            f, extra, ENTROPY_DEFAULT_THRESHOLD
        ):
            findings.append(
                {
                    "detector": f"redact:{pattern_name}",
                    "verified": False,
                    # redact.py performs no verification either, and the
                    # hook applies is_false_positive with the same flag —
                    # keeping the two gates in lockstep.
                    "conclusive": False,
                    "raw": matched,
                    "file": str(f),
                    "line": line_no,
                }
            )
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--brain-root", required=True, type=Path)
    ap.add_argument("--scanner", choices=("trufflehog", "gitleaks"),
                    default="trufflehog")
    ap.add_argument(
        "--no-redact", action="store_true",
        help="Skip the redact.py pass (trufflehog/gitleaks only).",
    )
    args = ap.parse_args()

    brain_root = args.brain_root.expanduser()
    allowlist = load_allowlist(brain_root)

    try:
        runner = _run_trufflehog if args.scanner == "trufflehog" else _run_gitleaks
        findings = runner(brain_root)
        if not args.no_redact:
            findings += _run_redact(brain_root)
    except (RuntimeError, FileNotFoundError, OSError) as e:
        # Fail closed — sync.sh refuses to push on exit 2.
        sys.stderr.write(f"scan_gate: scan failed: {e}\n")
        return 2

    quarantine: dict[str, list[dict]] = {}
    filtered = 0
    for f in findings:
        rel = _rel(f["file"], brain_root)
        if rel is None:
            continue
        if not f["verified"]:
            reason = is_false_positive(
                f["raw"], allowlist,
                verification_conclusive=f.get("conclusive", False),
            )
            if reason:
                filtered += 1
                sys.stderr.write(
                    f"scan_gate: ignoring {f['detector']} in {rel}"
                    f":{f['line']} — {reason}\n"
                )
                continue
        quarantine.setdefault(rel, []).append(f)

    # sync.sh reads this list one path per line (bash cannot hold NUL bytes
    # in a variable, so NUL-separation is not available). A path containing
    # a newline would split into two bogus paths, and the real file would
    # stay staged and get pushed. Refuse the whole run instead: exit 2 makes
    # sync.sh fail closed. Brain paths are framework-generated, so this
    # should never fire — but "should never" is not a security guarantee.
    unsafe = [r for r in quarantine if "\n" in r or "\r" in r]
    if unsafe:
        sys.stderr.write(
            f"scan_gate: {len(unsafe)} quarantine path(s) contain a newline "
            f"and cannot be safely passed to sync.sh; refusing the run\n"
        )
        return 2

    for rel, hits in sorted(quarantine.items()):
        worst = "VERIFIED" if any(h["verified"] for h in hits) else "unverified"
        names = ", ".join(sorted({h["detector"] for h in hits}))
        sys.stderr.write(
            f"scan_gate: QUARANTINE {rel} — {len(hits)} {worst} hit(s) [{names}]\n"
        )
        print(rel)

    sys.stderr.write(
        f"scan_gate: {len(findings)} finding(s), {filtered} filtered as "
        f"false positives, {len(quarantine)} file(s) quarantined\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
