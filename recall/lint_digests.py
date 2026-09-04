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
  5. All three values are written double-quoted. They are strings and
     must read back as strings: a digest stem like `2026-03-24` or `true`
     parses as a date or a bool unquoted, which both re-opens the
     "missing" check on every run and puts a non-string `name` in the
     index.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from recall import lint
from recall.frontmatter import parse_path

DIGEST_REQUIRED: tuple[str, ...] = ("name", "description", "type")
DIGEST_TYPE = "digest"
DESCRIPTION_MAX = 200

_UNPARSEABLE_REASON = (
    "unparseable frontmatter — run `recall lint --repair` first"
)


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
    root = Path(root)
    if (root / "semantic").is_dir():
        memory_root = root
    elif (root / "memory" / "semantic").is_dir():
        memory_root = root / "memory"
    else:
        memory_root = root
    candidate = memory_root / "semantic" / "digests"
    return candidate if candidate.is_dir() else None


_NAME_SLUG_RE = re.compile(r"[^a-z0-9_.-]+")


def derive_name(file: Path) -> str:
    """Slugify `file.stem` into a `name` value. Existing digest stems are
    already slug-shaped and must pass through unchanged."""
    slug = _NAME_SLUG_RE.sub("-", file.stem.lower()).strip("-")
    return slug[:120] or "digest"


def _humanized_stem(file: Path) -> str:
    """Best-effort human-readable fallback when a digest body has
    neither an H1 nor a usable first paragraph."""
    text = re.sub(r"[_\-]+", " ", file.stem).strip()
    return text or "digest"


_H1_RE = re.compile(r"(?m)^#\s+(.+?)\s*$")
_SKIP_LINE_RE = re.compile(r"^(#|_\(|-\s|\*\s|\||```|>)")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _truncate_description(text: str) -> str:
    if len(text) <= DESCRIPTION_MAX:
        return text
    cut = text[: DESCRIPTION_MAX - 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    cut = cut.rstrip()
    return cut + "…"


def derive_description(body: str, *, fallback: str) -> str:
    """`f"{h1} — {first_sentence}"` derived from the digest body (H1 +
    first prose sentence of the first real paragraph, skipping headings/
    bullets/quotes/fences/tables), truncated to `DESCRIPTION_MAX` on a
    word boundary. Falls back to whichever half exists, else `fallback`.
    """
    h1_match = _H1_RE.search(body)
    h1 = h1_match.group(1).strip() if h1_match else ""
    remainder = body[h1_match.end():] if h1_match else body

    para_lines: list[str] = []
    in_fence = False
    started = False
    for line in remainder.split("\n"):
        stripped = line.strip()
        if not started:
            if not stripped:
                continue  # skip leading blank lines
            if _SKIP_LINE_RE.match(stripped):
                if stripped.startswith("```"):
                    in_fence = not in_fence
                continue
            if in_fence:
                continue
            started = True
            para_lines.append(line)
        else:
            if not stripped or _SKIP_LINE_RE.match(stripped):
                break
            para_lines.append(line)

    para = re.sub(r"\s+", " ", "\n".join(para_lines)).strip()
    sentence = _SENTENCE_SPLIT_RE.split(para, 1)[0].strip() if para else ""

    if h1 and sentence:
        result = f"{h1} — {sentence}"
    elif h1:
        result = h1
    elif sentence:
        result = sentence
    else:
        result = fallback

    return _truncate_description(result)


def plan_digest_fixes(root: Path) -> list[DigestFix]:
    """One `DigestFix` per digest under `digests_dir(root)` (sorted by
    path, symlinks skipped): missing-keys + proposed values, or a
    `skipped_reason` when the frontmatter doesn't parse."""
    d = digests_dir(root)
    if d is None:
        return []

    fixes: list[DigestFix] = []
    for file in sorted(d.glob("*.md")):
        if file.is_symlink():
            continue

        if lint._check_unparseable_frontmatter(file):
            fixes.append(DigestFix(
                file=file, missing=(), proposed={},
                skipped_reason=_UNPARSEABLE_REASON,
            ))
            continue

        parsed = parse_path(file)
        fm = parsed.frontmatter
        missing = tuple(
            k for k in DIGEST_REQUIRED
            if not (isinstance(fm.get(k), str) and fm.get(k).strip())
        )
        if not missing:
            continue

        proposed: dict[str, str] = {}
        for key in missing:
            if key == "name":
                proposed["name"] = derive_name(file)
            elif key == "type":
                proposed["type"] = DIGEST_TYPE
            elif key == "description":
                proposed["description"] = derive_description(
                    parsed.body, fallback=_humanized_stem(file))
        fixes.append(DigestFix(
            file=file, missing=missing, proposed=proposed, skipped_reason="",
        ))

    return fixes


def _fm_line(key: str, value: str) -> str:
    """One `key: "value"` frontmatter line, YAML-safe.

    Every backfilled value is a string, so every one is quoted — the
    writer and the manifest both go through here so the preview can never
    drift from what lands on disk.
    """
    return f"{key}: {lint._double_quote(value)}"


def apply_digest_fixes(fixes: list[DigestFix], *, dry_run: bool = True) -> list[Path]:
    """Splice each fix's proposed keys into its file's existing
    frontmatter block. Dry-run by default. Returns the files actually
    written."""
    if dry_run:
        return []

    written: list[Path] = []
    for fix in fixes:
        if fix.skipped_reason or not fix.proposed:
            continue

        bounds = lint._read_frontmatter_bounds(fix.file)
        if bounds is None:
            # Defensive: no digest today lacks a real frontmatter block, but
            # never fabricate one — skip rather than guess at a shape.
            continue
        raw, newline, body_start, _fm_end = bounds

        insert_lines = [
            _fm_line(key, fix.proposed[key])
            for key in ("name", "description", "type")
            if key in fix.proposed
        ]
        if not insert_lines:
            continue

        insertion = newline.join(insert_lines) + newline
        new_raw = raw[:body_start] + insertion + raw[body_start:]
        if lint._atomic_write(fix.file, new_raw):
            written.append(fix.file)

    return written


def _rel_display(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(Path(root)))
    except ValueError:
        return path.name


def render_digest_manifest(
    fixes: list[DigestFix], root: Path, *, applied: list[Path] | None = None
) -> str:
    """Human-readable `== recall lint --fix-digests ==` manifest, or the
    post-apply summary line when `applied` is given."""
    lines: list[str] = []

    d = digests_dir(root)
    if d is not None:
        total = sum(1 for f in sorted(d.glob("*.md")) if not f.is_symlink())
        rel_dir = _rel_display(d, root)
        lacking = sum(1 for f in fixes if f.proposed)
        lines.append(
            f"== recall lint --fix-digests ==  ({total} digests under {rel_dir}; "
            f"{lacking} lack name/description/type)"
        )
    else:
        lines.append("== recall lint --fix-digests ==  (0 digests found)")

    changed = 0
    skipped = 0
    for fix in fixes:
        rel = _rel_display(fix.file, root)
        if fix.skipped_reason:
            skipped += 1
            lines.append(f"  {rel}")
            lines.append(f"    skipped: {fix.skipped_reason}")
            continue
        if not fix.proposed:
            continue
        changed += 1
        lines.append(f"  {rel}")
        for key in ("name", "description", "type"):
            if key in fix.proposed:
                lines.append(f"    + {_fm_line(key, fix.proposed[key])}")

    lines.append("")
    if applied is not None:
        lines.append(f"Backfilled frontmatter on {len(applied)} digest(s).")
    else:
        lines.append(
            f"{changed} file(s) would change, {skipped} skipped. "
            f"Re-run with --fix-digests --apply to write."
        )

    return "\n".join(lines)
