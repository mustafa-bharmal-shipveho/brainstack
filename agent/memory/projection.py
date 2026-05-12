"""Project materialized claim state into the semantic markdown tree.

Each `ClaimRecord` in the materialized state becomes one
`<brainRoot>/memory/semantic/claims/<claim_id>.md` file with frontmatter:

  ---
  type: claim-current   # or claim-stale | claim-tombstone
  topic_key: project:ps2
  claim_subject: release-date
  source: research-notes
  source_event_id: rn:1
  source_ts_epoch: 1700000000.0
  superseded_by: <new_claim_id>   # or null
  stance: current   # or superseded | tombstone
  claim_id: <hash>
  ---

  <value_raw>

Recall's existing `discover_documents` walker picks these up automatically
because they live under `semantic/` and end in `.md`. The frontmatter
`type` field lets `recall query` filter by `claim-current` (default) or
include `claim-stale` / `claim-tombstone` when the operator opts in.

Reconcile semantics: every run rebuilds the directory from materialized
state. Files for claim_ids not in `state.claims_by_id` are deleted.
Existing files whose contents are byte-identical to what we'd write are
left untouched (mtime stable → no churn in the recall index).

Atomicity: per-file via `_atomic.atomic_write_bytes`. Directory-level
race tolerance: `discover_documents` already swallows `OSError` for
missing files, so a half-written reconciliation never breaks a
concurrent read.

Framework rule: NEVER inspect `event["source"]` or hardcode producer
names. Source field is rendered as opaque metadata only.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import claims
from _atomic import atomic_write_bytes


_NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def _claims_dir(brain_root: str, namespace: str = "default") -> str:
    """Resolve the per-namespace claims projection directory."""
    if namespace != "default" and not _NAMESPACE_RE.match(namespace or ""):
        raise ValueError(f"invalid namespace: {namespace!r}")
    root = os.path.abspath(brain_root)
    if namespace == "default":
        return os.path.join(root, "memory", "semantic", "claims")
    return os.path.join(root, "memory", "semantic", namespace, "claims")


def _stance_to_type(stance: str) -> str:
    """Map ClaimRecord.stance → frontmatter `type` value."""
    if stance == claims.STANCE_CURRENT:
        return "claim-current"
    if stance == claims.STANCE_TOMBSTONE:
        return "claim-tombstone"
    return "claim-stale"


def _render_yaml_value(v: Any) -> str:
    """Tiny stdlib-only YAML value renderer.

    Handles the shapes we actually write (str / int / float / None /
    bool). Strings are quoted iff they contain YAML-special chars; we
    err on the side of always-quoting to avoid edge cases. We do NOT
    pull in pyyaml — projection runs from launchd under the system
    python which doesn't have it installed.
    """
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    # String. Escape backslash + double-quote and wrap in double quotes.
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _render_frontmatter(fm: Dict[str, Any]) -> str:
    """Render an OrderedDict-style frontmatter to YAML-compatible text.

    Recall's frontmatter parser (recall/frontmatter.py) accepts simple
    `key: value` lines plus strict YAML for nested shapes — ours is
    flat so the line-by-line writer is sufficient.
    """
    out = []
    for key in sorted(fm.keys()):
        out.append(f"{key}: {_render_yaml_value(fm[key])}")
    return "\n".join(out) + "\n"


def _render_claim_markdown(rec: claims.ClaimRecord) -> bytes:
    """Render a single claim as frontmattered markdown bytes."""
    fm: Dict[str, Any] = {
        "type": _stance_to_type(rec.stance),
        "topic_key": rec.topic_key,
        "claim_subject": rec.claim_subject,
        "source": rec.source,
        "source_event_id": rec.source_event_id,
        "source_ts_epoch": rec.source_ts_epoch,
        "superseded_by": rec.superseded_by,
        "stance": rec.stance,
        "claim_id": rec.claim_id,
        "value_normalized": rec.value_normalized,
        # `name` and `description` are used by recall/sources.py to
        # weight the indexed text — give the projection meaningful
        # values rather than relying on the file stem.
        "name": f"{rec.topic_key} / {rec.claim_subject}",
        "description": rec.value_normalized,
    }
    front = _render_frontmatter(fm)
    body = rec.value_raw or rec.value_normalized or ""
    text = f"---\n{front}---\n\n{body.rstrip()}\n"
    return text.encode("utf-8")


@dataclass
class ProjectionResult:
    written: int = 0
    skipped_unchanged: int = 0
    deleted_orphans: int = 0


def project_to_markdown_reconcile(state: claims.ClaimState,
                                  brain_root: str,
                                  namespace: str = "default",
                                  include_stale: bool = False,
                                  ) -> ProjectionResult:
    """Rebuild the claims projection directory from `state`.

    By default, projects ONLY current claims so a plain `recall query`
    (which has no awareness of the new `type: claim-*` frontmatter
    flag) returns only authoritative facts [codex PR4 P1 fix].

    Pass `include_stale=True` to also project superseded and tombstone
    claims into the same directory. This is the operator opt-in for
    archaeological queries; a future `recall query --include-
    superseded` CLI flag will set this.

    Idempotent: re-running with the same state yields a byte-identical
    directory and ZERO orphan deletions. Crash-safe: each write is atomic
    via `_atomic`, and orphan removal only runs after every expected file
    is written.
    """
    out_dir = _claims_dir(brain_root, namespace)
    os.makedirs(out_dir, exist_ok=True)

    result = ProjectionResult()
    expected_filenames: set = set()

    for claim_id, rec in state.claims_by_id.items():
        if not include_stale and rec.stance != claims.STANCE_CURRENT:
            # Skip stale + tombstone — they're audit-only data and would
            # otherwise pollute default `recall query` results.
            continue
        filename = f"{claim_id}.md"
        expected_filenames.add(filename)
        path = os.path.join(out_dir, filename)
        payload = _render_claim_markdown(rec)
        # Skip rewrite if contents are identical (mtime stability — the
        # recall index uses mtime to detect refresh need).
        existing: Optional[bytes] = None
        try:
            with open(path, "rb") as f:
                existing = f.read()
        except OSError:
            existing = None
        if existing == payload:
            result.skipped_unchanged += 1
            continue
        atomic_write_bytes(path, payload)
        result.written += 1

    # Delete orphans: any *.md in the dir that's not expected.
    try:
        present = os.listdir(out_dir)
    except OSError:
        present = []
    for name in present:
        if not name.endswith(".md"):
            continue
        if name in expected_filenames:
            continue
        try:
            os.remove(os.path.join(out_dir, name))
            result.deleted_orphans += 1
        except OSError:
            pass

    return result
