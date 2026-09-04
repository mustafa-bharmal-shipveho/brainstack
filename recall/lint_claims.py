"""`recall lint --dedupe-claims` — archive duplicate/stub claims.

The `.md` files under `memory/semantic/claims/` are a PROJECTION of
`claims.jsonl`: every consolidation run rebuilds the directory and
deletes orphans. Moving a claim file to `archived/` without retracting
the claim gets it re-created by the next dream cycle. The sticky
mechanism is the operator override log —
`memory/semantic/claim_overrides.jsonl` — which survives even
`rm claims.jsonl`.

So an archive here is three writes that must all succeed before the
original is unlinked:
  1. an archived copy carrying a tombstone note and `needs_review: true`
  2. a `retract` row in `claim_overrides.jsonl`, byte-identical to what
     `claim_overrides.retract_by_claim_id` produces
  3. only then, `os.unlink` of the original

If any step fails, the original stays — losing a claim is worse than
keeping a duplicate.

Rules:
  - `duplicate_source_event`: files sharing a `source_event_id`, keep
    the oldest (by source_ts_epoch, then mtime, then claim_id)
  - `stub_slack_point`: a body that is nothing but a Slack permalink
  - `stub_unknown_value`: `value_normalized: unknown` — a claim with no
    resolved content
  - `stub_short_body`: OFF by default (`STUB_MIN_CHARS == 0`). Claims
    are one-liners by construction (median body 65 chars), so length
    alone is not a content-free signal; the rule only fires behind an
    explicit `--stub-min-chars N`.
  - precedence per file: duplicate > slack_point > unknown_value > short_body
"""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from recall.frontmatter import parse_file_text, parse_path
from recall.lint import _atomic_write, _double_quote, mark_needs_review

# Claims are one-liners by construction (median body 65 chars); length
# alone is not a content-free signal, so this rule ships disabled unless
# the operator opts in via `--stub-min-chars`.
STUB_MIN_CHARS = 0

# Anchored at the start of the body: a claim that merely CITES a Slack
# thread is real content, only a bare `point: <slack permalink>` stub
# matches.
_SLACK_POINT_RE = re.compile(
    r"^point:\s*<https?://[^\s>]*slack\.com/[^\s>]*>", re.IGNORECASE
)

# `value_normalized` the extractor could not resolve — a claim with a
# subject but no content.
_UNKNOWN_VALUE = "unknown"

REASON_DUPLICATE = "duplicate_source_event"
REASON_SLACK_POINT = "stub_slack_point"
REASON_UNKNOWN_VALUE = "stub_unknown_value"
REASON_SHORT_BODY = "stub_short_body"

# Highest precedence first. A file gets exactly one action: if it is a
# duplicate, that is what the manifest says, because "a sibling survives"
# is the reason it is safe to drop.
_REASON_ORDER = (
    REASON_DUPLICATE,
    REASON_SLACK_POINT,
    REASON_UNKNOWN_VALUE,
    REASON_SHORT_BODY,
)

_REASON_LABEL = {
    REASON_DUPLICATE: "duplicate",
    REASON_SLACK_POINT: "slack stub",
    REASON_UNKNOWN_VALUE: "unknown value",
    REASON_SHORT_BODY: "short",
}

# The actor recorded on every retraction row we append, so an operator
# reading claim_overrides.jsonl can tell machine dedupe from a human
# decision.
OVERRIDE_ACTOR = "recall-lint-dedupe"

# Mirrors `agent/memory/claim_overrides.CURRENT_SCHEMA`. Duplicated (not
# imported) because `agent/memory` is not importable from `recall`; the
# byte-identity of a row is pinned by
# tests/recall/test_lint_dedupe_claims.py::
# test_retraction_row_is_byte_identical_to_retract_by_claim_id.
_OVERRIDE_SCHEMA_VERSION = 1

# `%f` matches claim_overrides._now_iso(); the tombstone uses the coarser
# second-resolution stamp because it is read by humans.
_OVERRIDE_TS_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"
_TOMBSTONE_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"
_ARCHIVE_NAME_TS_FMT = "%Y%m%dT%H%M%S"

# How much of a stub body to quote in the manifest.
_DETAIL_BODY_CHARS = 56

# Only `stub_short_body` details carry the threshold, so the manifest can
# recover it without the planner passing it through.
_SHORT_DETAIL_RE = re.compile(r"body \d+ chars < (\d+)$")


@dataclass(frozen=True)
class ClaimAction:
    file: Path
    claim_id: str
    source_event_id: str
    reason: str
    keep: Path | None
    detail: str


@dataclass(frozen=True)
class _Claim:
    """One parsed claim file — everything the rules need, read once."""

    file: Path
    claim_id: str
    source_event_id: str
    source_ts: float
    mtime: float
    body: str
    value_normalized: str


def _memory_root(root: Path) -> Path:
    """Accept either the memory root or the brain root.

    `recall lint --brain` defaults to `resolve_brain_home()` (the memory
    dir), but callers hand us `~/.agent` too.
    """
    root = Path(root).expanduser()
    if (root / "semantic").is_dir():
        return root
    if (root / "memory" / "semantic").is_dir():
        return root / "memory"
    return root


def claims_dir(root: Path) -> Path | None:
    """`<memory_root>/semantic/claims`, resolving `root` whether it is
    the memory root or the brain root. None if absent."""
    candidate = _memory_root(root) / "semantic" / "claims"
    return candidate if candidate.is_dir() else None


def _rel(path: Path, memory_root: Path) -> str:
    """Path as written in manifests and tombstones: relative to the memory
    root so nothing leaks an absolute home directory."""
    try:
        return path.relative_to(memory_root).as_posix()
    except ValueError:
        return path.as_posix()


def _as_float(value: object) -> float:
    """`source_ts_epoch` as a sortable float; a missing/unparseable stamp
    sorts last so a claim with a real timestamp always wins the keeper
    slot."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return math.inf


def _scalar_id(value: object) -> str:
    """A claim_id / source_event_id as the string that keys the override log.

    An all-digit id (`claim_id: 1111…`) is a valid sha256 hex digest, but
    unquoted YAML resolves it to an int. The override log keys on the
    string form, so normalize here rather than dropping the file.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return ""


def _string_claim_id_raw(raw: str, claim_id: str) -> str:
    """Quote a `claim_id` frontmatter value that YAML would resolve to an int.

    Only touches the archived COPY, and only when the value does not
    round-trip as a string: an archived claim whose id reads back as an
    integer can no longer be matched against the `claim_id` in
    `claim_overrides.jsonl`, which is the whole point of keeping it.
    """
    if isinstance(parse_file_text(raw).frontmatter.get("claim_id"), str):
        return raw
    open_m = re.match(r"---(\r\n|\r|\n)", raw)
    if not open_m:
        return raw
    body_start = open_m.end()
    close_m = re.search(r"(?:\r\n|\r|\n)---[ \t]*(?=\r\n|\r|\n|$)", raw[body_start:])
    if not close_m:
        return raw
    fm_end = body_start + close_m.start()
    quoted = _double_quote(claim_id)
    fm_region, n = re.subn(
        r"(?m)^claim_id:[ \t]*\S.*$",
        lambda _m: f"claim_id: {quoted}",
        raw[body_start:fm_end],
        count=1,
    )
    if not n:
        return raw
    return raw[:body_start] + fm_region + raw[fm_end:]


def _scan_claims(cdir: Path) -> list[_Claim]:
    """Every claim file that carries the two keys the rules key on.

    A file without `claim_id` or `source_event_id` is not groupable and
    never actioned — we cannot retract what we cannot name.
    """
    claims: list[_Claim] = []
    for file in sorted(cdir.glob("*.md")):
        if file.is_symlink():
            continue  # never mutate a target outside the brain
        try:
            parsed = parse_path(file)
            mtime = file.stat().st_mtime
        except OSError:
            continue
        fm = parsed.frontmatter
        claim_id = _scalar_id(fm.get("claim_id"))
        source_event_id = _scalar_id(fm.get("source_event_id"))
        if not claim_id or not source_event_id:
            continue
        value = fm.get("value_normalized")
        claims.append(_Claim(
            file=file,
            claim_id=claim_id,
            source_event_id=source_event_id,
            source_ts=_as_float(fm.get("source_ts_epoch")),
            mtime=mtime,
            body=parsed.body.strip(),
            value_normalized=value.strip() if isinstance(value, str) else "",
        ))
    return claims


def _stub_reason(claim: _Claim, stub_min_chars: int) -> str | None:
    if _SLACK_POINT_RE.match(claim.body):
        return REASON_SLACK_POINT
    if claim.value_normalized.lower() == _UNKNOWN_VALUE:
        return REASON_UNKNOWN_VALUE
    if stub_min_chars > 0 and len(claim.body) < stub_min_chars:
        return REASON_SHORT_BODY
    return None


def _stub_detail(claim: _Claim, reason: str, stub_min_chars: int) -> str:
    if reason == REASON_SLACK_POINT:
        head = claim.body[:_DETAIL_BODY_CHARS]
        if len(claim.body) > _DETAIL_BODY_CHARS:
            head += "…"
        return f'body starts "{head}"'
    if reason == REASON_UNKNOWN_VALUE:
        return "value_normalized: unknown — the extractor resolved no content"
    return f"body {len(claim.body)} chars < {stub_min_chars}"


def plan_claim_dedupe(root: Path, *, stub_min_chars: int = STUB_MIN_CHARS) -> list[ClaimAction]:
    """One `ClaimAction` per claim file that should be archived (sorted,
    symlinks skipped, at most one action per file, duplicate takes
    precedence over the stub rules)."""
    cdir = claims_dir(root)
    if cdir is None:
        return []
    memory_root = _memory_root(root)
    claims = _scan_claims(cdir)

    groups: dict[str, list[_Claim]] = {}
    for claim in claims:
        groups.setdefault(claim.source_event_id, []).append(claim)

    actions: dict[Path, ClaimAction] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        # Oldest wins: the claim the extractor emitted first is the one
        # downstream memories already reference.
        keeper = min(members, key=lambda c: (c.source_ts, c.mtime, c.claim_id))
        for claim in members:
            if claim is keeper:
                continue
            actions[claim.file] = ClaimAction(
                file=claim.file,
                claim_id=claim.claim_id,
                source_event_id=claim.source_event_id,
                reason=REASON_DUPLICATE,
                keep=keeper.file,
                detail=(f"source_event_id {claim.source_event_id} · "
                        f"keep {_rel(keeper.file, memory_root)}"),
            )

    # Stub checks run on keepers too: the two live Slack "point:" stubs
    # share an event id, so the survivor of that group is itself junk.
    for claim in claims:
        if claim.file in actions:
            continue
        reason = _stub_reason(claim, stub_min_chars)
        if reason is None:
            continue
        actions[claim.file] = ClaimAction(
            file=claim.file,
            claim_id=claim.claim_id,
            source_event_id=claim.source_event_id,
            reason=reason,
            keep=None,
            detail=_stub_detail(claim, reason, stub_min_chars),
        )

    return [actions[f] for f in sorted(actions)]


def _tombstone(
    action: ClaimAction, memory_root: Path, *, keep_rel: str, stamp: str
) -> str:
    return (
        "\n"
        f"<!-- tombstone: archived by `recall lint --dedupe-claims --apply` at {stamp}\n"
        f"     reason: {action.reason}\n"
        f"     source_event_id: {action.source_event_id}\n"
        f"     kept: {keep_rel}\n"
        f"     original: {_rel(action.file, memory_root)}\n"
        "     retraction: appended to semantic/claim_overrides.jsonl "
        "(key claim_id) -->\n"
    )


def _archive_path(archived_dir: Path, claim_id: str, stamp: str) -> Path:
    """`<ts>-claim-<cid[:16]>.md`, uniquified only if that name is taken."""
    base = f"{stamp}-claim-{claim_id[:16]}"
    dest = archived_dir / f"{base}.md"
    n = 2
    while dest.exists():
        dest = archived_dir / f"{base}-{n}.md"
        n += 1
    return dest


def _keeper_claim_ids(actions: list[ClaimAction]) -> dict[Path, str]:
    """Resolve every keeper's claim_id BEFORE anything is unlinked.

    A keeper can itself be archived as a stub (the Slack pair), so reading
    it lazily during the apply loop would race with its own deletion.
    """
    known = {a.file: a.claim_id for a in actions}
    resolved: dict[Path, str] = {}
    for action in actions:
        if action.keep is None or action.keep in resolved:
            continue
        if action.keep in known:
            resolved[action.keep] = known[action.keep]
            continue
        try:
            cid = parse_path(action.keep).frontmatter.get("claim_id")
        except Exception:
            cid = None
        resolved[action.keep] = (
            cid.strip() if isinstance(cid, str) and cid.strip() else action.keep.stem
        )
    return resolved


def apply_claim_dedupe(
    actions: list[ClaimAction],
    root: Path,
    *,
    dry_run: bool = True,
    now: datetime | None = None,
) -> list[Path]:
    """Archive + retract + unlink each action, in that order, aborting
    (and reporting) an action if any step fails. Dry-run by default.
    Returns the archived-copy paths actually written."""
    if dry_run or not actions:
        return []

    memory_root = _memory_root(root)
    archived_dir = memory_root / "semantic" / "archived"
    overrides_path = memory_root / "semantic" / "claim_overrides.jsonl"

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    tombstone_stamp = now.strftime(_TOMBSTONE_TS_FMT)
    name_stamp = now.strftime(_ARCHIVE_NAME_TS_FMT)
    override_stamp = now.strftime(_OVERRIDE_TS_FMT)

    keeper_ids = _keeper_claim_ids(actions)

    try:
        archived_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return []

    applied: list[Path] = []
    for action in actions:
        try:
            raw = action.file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        keep_rel = _rel(action.keep, memory_root) if action.keep else "none"
        keep_id = keeper_ids.get(action.keep) if action.keep else None
        dest = _archive_path(archived_dir, action.claim_id, name_stamp)

        # 1. the archived copy (never overwrite the original in place)
        raw = _string_claim_id_raw(raw, action.claim_id)
        body = raw if raw.endswith("\n") else raw + "\n"
        body += _tombstone(action, memory_root, keep_rel=keep_rel, stamp=tombstone_stamp)
        if not _atomic_write(dest, body):
            continue  # nothing written, nothing to roll back

        try:
            # 2. demote the copy so it stays out of results even if the
            #    `semantic/archived/**` source exclude is missing.
            mark_needs_review([dest])
            # 3. the retraction — without it the next projection run
            #    recreates the file we are about to delete.
            _append_override_retract(
                overrides_path,
                claim_id=action.claim_id,
                note=f"{action.reason}; kept={keep_id or 'none'}",
                now_iso=override_stamp,
            )
        except Exception:
            _unlink_quietly(dest)
            continue

        # 4. only now is it safe to remove the original.
        try:
            os.unlink(action.file)
        except OSError:
            _unlink_quietly(dest)
            continue
        applied.append(dest)

    return applied


def _unlink_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _infer_stub_min_chars(actions: list[ClaimAction]) -> int:
    """Recover the threshold the plan ran with from its own actions.

    `render_claim_manifest` is called by the CLI without the flag value,
    and only `stub_short_body` details carry it. With the rule off (the
    default) there is nothing to recover and nothing to report — 0.
    """
    for action in actions:
        if action.reason != REASON_SHORT_BODY:
            continue
        m = _SHORT_DETAIL_RE.search(action.detail)
        if m:
            return int(m.group(1))
    return STUB_MIN_CHARS


def render_claim_manifest(
    actions: list[ClaimAction], root: Path, *, applied: list[Path] | None = None
) -> str:
    """Human-readable `== recall lint --dedupe-claims ==` manifest, or
    the post-apply summary line when `applied` is given."""
    memory_root = _memory_root(root)
    lines: list[str] = []

    if applied is None:
        cdir = claims_dir(root)
        claims = _scan_claims(cdir) if cdir is not None else []
        n_events = len({c.source_event_id for c in claims})
        lines.append(
            f"== recall lint --dedupe-claims ==  ({len(claims)} claims, "
            f"{n_events} distinct source events; "
            f"stub_min_chars={_infer_stub_min_chars(actions)})"
        )
    else:
        lines.append("== recall lint --dedupe-claims ==")

    for action in actions:
        lines.append(
            f"  ARCHIVE  {action.reason:<22s}  {_rel(action.file, memory_root)}"
        )
        lines.append(f"           {action.detail}")

    if applied is not None:
        lines.append("")
        lines.append(
            f"Archived {len(applied)} claim(s) to semantic/archived/ and appended "
            f"{len(applied)} retraction(s) to semantic/claim_overrides.jsonl."
        )
        skipped = len(actions) - len(applied)
        if skipped > 0:
            lines.append(
                f"{skipped} claim(s) left in place — the archive or the retraction "
                f"failed, so the original was not deleted."
            )
        return "\n".join(lines)

    counts = {reason: 0 for reason in _REASON_ORDER}
    for action in actions:
        counts[action.reason] = counts.get(action.reason, 0) + 1
    breakdown = ", ".join(
        f"{counts[r]} {_REASON_LABEL[r]}" for r in _REASON_ORDER if counts.get(r)
    )
    tail = f" ({breakdown})" if breakdown else ""
    lines.append("")
    lines.append(
        f"{len(actions)} file(s) would be archived to semantic/archived/{tail}. "
        f"Re-run with --dedupe-claims --apply to write."
    )
    return "\n".join(lines)


def _append_override_retract(
    overrides_path: Path, *, claim_id: str, note: str, now_iso: str
) -> None:
    """Append one `{"op": "retract", "key_type": "claim_id", ...}` row —
    byte-identical (same keys/values) to what
    `claim_overrides.retract_by_claim_id` produces — under
    `fcntl.LOCK_EX` on `<path>.lock`."""
    row = {
        "op": "retract",
        "key_type": "claim_id",
        "claim_id": claim_id,
        "actor": OVERRIDE_ACTOR,
        "note": note,
        "schema_version": _OVERRIDE_SCHEMA_VERSION,
        "at": now_iso,
    }
    payload = (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")

    overrides_path.parent.mkdir(parents=True, exist_ok=True)
    sentinel = str(overrides_path) + ".lock"

    try:
        import fcntl
    except ImportError:  # pragma: no cover — Windows
        with open(overrides_path, "ab") as fh:
            fh.write(payload)
            fh.flush()
        return

    lock_fd = os.open(sentinel, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        with open(overrides_path, "ab") as fh:
            fh.write(payload)
            fh.flush()
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
