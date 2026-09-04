"""`recall lint --fix-digests` — backfill name/description/type onto
session digests (memory/semantic/digests/*.md).

Every session digest HAS a frontmatter block (session_id, source,
started_at, cwd, domain_tags, outcome, salience) but none carry
`name` / `description` / `type`. Recall weights `name` + `description`
when it builds the indexed text and uses `type` for filtering, so a
digest without them is retrievable only by its raw body. This module
plans and applies the backfill.

Hard constraints:
  1. Splices into the EXISTING frontmatter block. Never fabricates a
     block, never rewrites the body, never touches the file's newline
     style.
  2. Only proposes the MISSING keys — a second run is a no-op.
  3. Dry-run by default; writing requires `apply_digest_fixes(...,
     dry_run=False)`.
  4. A digest whose frontmatter YAML doesn't parse is SKIPPED with a
     reason, never guessed at — `recall lint --repair` owns that case.

Scaffold module: constants + the `DigestFix` dataclass are real; every
function below is a stub (`raise NotImplementedError("scaffold")`). See
tests/recall/test_lint_fix_digests.py for the pinned contract.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DIGEST_REQUIRED: tuple[str, ...] = ("name", "description", "type")
DIGEST_TYPE = "digest"
DESCRIPTION_MAX = 200


@dataclass(frozen=True)
class DigestFix:
    file: Path
    missing: tuple[str, ...]
    proposed: dict[str, str]
    skipped_reason: str = ""


def digests_dir(root: Path) -> Path | None:
    """`<memory_root>/semantic/digests`, resolving `root` whether it is
    the memory root or the brain root (mirrors lint's `--brain` default
    of `resolve_brain_home()` == `~/.agent/memory`). None if absent."""
    raise NotImplementedError("scaffold")


def derive_name(file: Path) -> str:
    """Slugify `file.stem` into a `name` value. Existing digest stems are
    already slug-shaped and must pass through unchanged."""
    raise NotImplementedError("scaffold")


def derive_description(body: str, *, fallback: str) -> str:
    """`f"{h1} — {first_sentence}"` derived from the digest body (H1 +
    first prose sentence of the first real paragraph, skipping headings/
    bullets/quotes/fences/tables), truncated to `DESCRIPTION_MAX` on a
    word boundary. Falls back to whichever half exists, else `fallback`.
    """
    raise NotImplementedError("scaffold")


def plan_digest_fixes(root: Path) -> list[DigestFix]:
    """One `DigestFix` per digest under `digests_dir(root)` (sorted by
    path, symlinks skipped): missing-keys + proposed values, or a
    `skipped_reason` when the frontmatter doesn't parse."""
    raise NotImplementedError("scaffold")


def apply_digest_fixes(fixes: list[DigestFix], *, dry_run: bool = True) -> list[Path]:
    """Splice each fix's proposed keys into its file's existing
    frontmatter block. Dry-run by default. Returns the files actually
    written."""
    raise NotImplementedError("scaffold")


def render_digest_manifest(
    fixes: list[DigestFix], root: Path, *, applied: list[Path] | None = None
) -> str:
    """Human-readable `== recall lint --fix-digests ==` manifest, or the
    post-apply summary line when `applied` is given."""
    raise NotImplementedError("scaffold")
