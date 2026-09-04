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

Scaffold module: constants + the `ClaimAction` dataclass are real; every
function below is a stub (`raise NotImplementedError("scaffold")`). See
tests/recall/test_lint_dedupe_claims.py for the pinned contract.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

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


@dataclass(frozen=True)
class ClaimAction:
    file: Path
    claim_id: str
    source_event_id: str
    reason: str
    keep: Path | None
    detail: str


def claims_dir(root: Path) -> Path | None:
    """`<memory_root>/semantic/claims`, resolving `root` whether it is
    the memory root or the brain root. None if absent."""
    raise NotImplementedError("scaffold")


def plan_claim_dedupe(root: Path, *, stub_min_chars: int = STUB_MIN_CHARS) -> list[ClaimAction]:
    """One `ClaimAction` per claim file that should be archived (sorted,
    symlinks skipped, at most one action per file, duplicate takes
    precedence over the stub rules)."""
    raise NotImplementedError("scaffold")


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
    raise NotImplementedError("scaffold")


def render_claim_manifest(
    actions: list[ClaimAction], root: Path, *, applied: list[Path] | None = None
) -> str:
    """Human-readable `== recall lint --dedupe-claims ==` manifest, or
    the post-apply summary line when `applied` is given."""
    raise NotImplementedError("scaffold")


def _append_override_retract(
    overrides_path: Path, *, claim_id: str, note: str, now_iso: str
) -> None:
    """Append one `{"op": "retract", "key_type": "claim_id", ...}` row —
    byte-identical (same keys/values) to what
    `claim_overrides.retract_by_claim_id` produces — under
    `fcntl.LOCK_EX` on `<path>.lock`."""
    raise NotImplementedError("scaffold")
