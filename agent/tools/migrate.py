#!/usr/bin/env python3
"""Migrate a flat memory directory into the 4-layer brain structure.

Source layout (Claude Code's auto-memory or any flat dir of typed `.md` files):

    <source>/
        MEMORY.md           # index (rewritten)
        feedback_*.md       # graduated lessons
        user_*.md           # personal profile
        project_*.md        # project context
        cycle-*.md          # session reflections
        reference_*.md      # external resource pointers
        <other>.md          # misc

Target layout (~/.agent/-shaped):

    <target>/memory/
        semantic/
            lessons.jsonl              # graduated lesson rows (extension fields preserved)
            lessons/<slug>.md          # long-form companions
        personal/
            profile/<slug>.md          # user_*.md
            notes/<slug>.md            # project_*, cycle-*, misc
            references/<slug>.md       # reference_*.md
        MEMORY.md                      # rewritten index pointing at new locations

Idempotent: rerunning on the same source produces the same target. Lessons
are keyed by source filename, so duplicate runs do not double-append.

Usage:
    python3 migrate.py <source-flat-dir> <target-brain-root>
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import sys
from pathlib import Path

# Atomic writes — must match the v0.1.1 hardening of auto_dream / promote /
# review_state so a SIGKILL during migration doesn't leave torn jsonl/md.
_BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_BASE, "memory"))
from _atomic import atomic_write_bytes, atomic_write_text  # noqa: E402  (path-relative import)


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Extract `---\\nkey: value\\n...\\n---` block. Returns (meta, body)."""
    meta: dict[str, str] = {}
    if not text.startswith("---\n"):
        return meta, text
    try:
        end = text.index("\n---\n", 4)
    except ValueError:
        return meta, text
    fm = text[4:end]
    for line in fm.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    body = text[end + 5 :]
    return meta, body


def parse_feedback(body: str) -> tuple[str, str, str]:
    """Split a feedback file body into (claim, why, how_to_apply).

    Recognizes `**Why:**` and `**How to apply:**` markers (case-insensitive,
    bold or bare). Anything before the first marker is the claim.
    """
    # Find marker positions
    why_re = re.compile(r"\*\*Why:?\*\*|^Why:", re.IGNORECASE | re.MULTILINE)
    how_re = re.compile(
        r"\*\*How to apply:?\*\*|^How to apply:", re.IGNORECASE | re.MULTILINE
    )

    why_match = why_re.search(body)
    how_match = how_re.search(body)

    # Determine slice boundaries
    earliest = min(
        m.start() for m in (why_match, how_match) if m
    ) if (why_match or how_match) else len(body)

    claim = body[:earliest].strip()

    # Extract why block
    why = ""
    if why_match:
        why_start = why_match.end()
        if how_match and how_match.start() > why_match.start():
            why_end = how_match.start()
        else:
            why_end = len(body)
        why = body[why_start:why_end].strip()

    # Extract how_to_apply block
    how = ""
    if how_match:
        how_start = how_match.end()
        if why_match and why_match.start() > how_match.start():
            how_end = why_match.start()
        else:
            how_end = len(body)
        how = body[how_start:how_end].strip()

    return claim.strip(), why.strip(), how.strip()


def slugify(stem: str, prefix: str) -> str:
    """Strip a known prefix from filename stem to get a slug."""
    if stem.startswith(prefix):
        return stem[len(prefix):]
    return stem


def lesson_id_from_stem(stem: str, rel_dir: Path | None = None) -> str:
    """Stable id derived from source filename — keeps re-migration idempotent.

    `rel_dir` is the source-relative directory (e.g. `semantic/lessons/sub`).
    When set, it's mixed into the hash so two feedback files with the same
    basename in different subdirs get distinct ids. Legacy flat callers pass
    `None` (or omit) and get the unchanged hash, preserving back-compat with
    pre-recursion brains where IDs are basename-only.
    """
    if rel_dir is None or rel_dir == Path("semantic") / "lessons":
        # Legacy: flat-source feedback or top-level nested-target feedback.
        # Hash basename only — matches IDs in pre-recursion brains.
        key = stem
    else:
        key = f"{rel_dir.as_posix()}/{stem}"
    h = hashlib.md5(key.encode()).hexdigest()[:12]
    return f"lesson_{h}"


def migrate_feedback(
    src_path: Path,
    target_root: Path,
    companion_rel_dir: Path | None = None,
) -> dict:
    """Convert one feedback_*.md file into a lesson row + companion markdown.

    `companion_rel_dir` is the relative directory under `<target>/memory/`
    where the companion `.md` lives. Defaults to `semantic/lessons` (legacy
    flat-source behavior). Pass a deeper path to preserve nested source
    structure (e.g. `semantic/lessons/sub` when source had that nesting).
    """
    text = src_path.read_text()
    meta, body = parse_frontmatter(text)
    claim, why, how_to_apply = parse_feedback(body)

    # Companion preserves the original filename (incl. `feedback_` prefix)
    # so audit / round-trip tracing back to the source is unambiguous.
    if companion_rel_dir is None:
        companion_rel_dir = Path("semantic") / "lessons"
    lid = lesson_id_from_stem(src_path.stem, rel_dir=companion_rel_dir)

    # Write companion markdown verbatim, atomically.
    companion_dir = target_root / "memory" / companion_rel_dir
    companion_dir.mkdir(parents=True, exist_ok=True)
    companion_path = companion_dir / src_path.name
    atomic_write_text(companion_path, text)

    # Use source file's mtime as accepted_at so re-running migrate is idempotent.
    # Falls back to "now" only when the file has no usable mtime (rare).
    try:
        mtime = src_path.stat().st_mtime
        accepted_at = datetime.datetime.fromtimestamp(
            mtime, tz=datetime.timezone.utc
        ).isoformat()
    except OSError:
        accepted_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # Claim is the rule itself — first paragraph of body. Description in
    # frontmatter is a summary, not the rule, so we don't use it as claim.
    # Critical: collapse newlines and stray `- ` bullets to spaces so the
    # rendered LESSONS.md bullet stays on one line. Multi-line claims would
    # cause migrate_legacy_bullets() in render_lessons.py to misread inner
    # bullets as separate top-level lessons (recursive duplication).
    body_first_para = claim.split("\n\n", 1)[0].strip()
    raw = body_first_para if body_first_para else (meta.get("description") or "").strip()
    # Replace newlines + leading bullet markers with separators
    claim_text = re.sub(r"\s*\n+\s*-\s*", " · ", raw)  # bullet on next line → · separator
    claim_text = re.sub(r"\s*\n+\s*", " ", claim_text)  # any remaining newlines → space
    claim_text = re.sub(r"\s{2,}", " ", claim_text).strip()
    # `original_markdown_path` records the COMPANION location under the new
    # brain — readers should always be able to find the long-form text from
    # the row without depending on the (possibly deleted) source dir.
    companion_rel = companion_rel_dir / src_path.name
    row = {
        "id": lid,
        "claim": claim_text[:500],
        "conditions": [],
        "evidence_ids": [],
        "status": "accepted",
        "accepted_at": accepted_at,
        "reviewer": "migrate.py",
        "rationale": f"Migrated from {src_path.name} on {accepted_at[:10]}",
        "cluster_size": 1,
        "canonical_salience": 7.0,
        "confidence": 0.7,
        "support_count": 0,
        "contradiction_count": 0,
        "supersedes": None,
        "source_candidate": None,
        # Extension fields:
        "why": why,
        "how_to_apply": how_to_apply,
        "original_markdown_path": str(companion_rel),
    }
    # Carry select frontmatter fields onto the structured row so future code
    # reading lessons.jsonl can filter without parsing markdown. Cap each
    # carried-through string at 200 chars to bound row size against
    # adversarial frontmatter (per security review #7). The companion .md
    # still preserves the full original frontmatter verbatim.
    if meta.get("name"):
        row["name"] = meta["name"][:200]
    if meta.get("type"):
        row["type"] = meta["type"][:200]
    # Frontmatter uses camelCase `originSessionId`; JSONL convention is
    # snake_case. Field name is `source_session_id` (NOT `origin_session_id`)
    # to avoid semantic collision with the v0.3 episode `origin` discriminator
    # (`coding.tool_call`, `agentry.<agent>.<event>`). See multitenant + schema
    # persona reviews.
    if meta.get("originSessionId"):
        row["source_session_id"] = meta["originSessionId"][:200]
    return row


def write_lessons_jsonl(lessons: list[dict], target_root: Path) -> Path:
    semantic_dir = target_root / "memory" / "semantic"
    semantic_dir.mkdir(parents=True, exist_ok=True)
    path = semantic_dir / "lessons.jsonl"
    # Idempotent: rewrite the file from scratch each run, sorted by id.
    # Atomic so SIGKILL during the rewrite leaves the previous file intact.
    by_id = {L["id"]: L for L in lessons}
    sorted_lessons = [by_id[k] for k in sorted(by_id)]
    body = "".join(json.dumps(L) + "\n" for L in sorted_lessons)
    atomic_write_text(path, body)
    return path


def write_simple_file(src_path: Path, target_dir: Path, slug_prefix: str = "") -> Path:
    """Copy file content to target_dir, stripping `slug_prefix` from filename.

    Uses `read_bytes` / atomic `write_bytes` so non-UTF-8 content (rare but
    real in user dirs) round-trips byte-for-byte and a SIGKILL during write
    leaves the target untouched.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    new_name = src_path.name
    if slug_prefix and new_name.startswith(slug_prefix):
        new_name = new_name[len(slug_prefix):]
    target_path = target_dir / new_name
    atomic_write_bytes(target_path, src_path.read_bytes())
    return target_path


def categorize(filename: str) -> str:
    """Return route category for a filename."""
    if filename == "MEMORY.md":
        return "index"
    if filename.startswith("feedback_"):
        return "feedback"
    if filename.startswith("user_"):
        return "user"
    if filename.startswith("project_"):
        return "project"
    if filename.startswith("cycle-") or filename.startswith("cycle_"):
        return "cycle"
    if filename.startswith("reference_"):
        return "reference"
    return "misc"


# Match `- [name](path) — optional hook text` index entries.
# Accept all three dash variants editors substitute: em-dash `—` (U+2014),
# en-dash `–` (U+2013), and double-dash `--`.
_INDEX_LINE = re.compile(
    r"^- \[(?P<name>[^\]]+)\]\((?P<path>[^)]+)\)"
    r"(?:\s+(?:—|–|--)\s+(?P<hook>.+))?\s*$"
)


def parse_index_hooks(index_path: Path) -> dict[str, str]:
    """Parse a MEMORY.md and return {file_stem: hook_text} for entries with hooks.

    Keyed by stem (basename without .md) so the map survives the source→target
    path rewrite. Returns empty dict if the index is missing or unparseable.
    Lines longer than 4096 chars are skipped (cheap defense against pathological
    input — see security review #9).
    """
    hooks: dict[str, str] = {}
    if not index_path.is_file():
        return hooks
    try:
        text = index_path.read_text()
    except (OSError, UnicodeDecodeError):
        return hooks
    for line in text.splitlines():
        if len(line) > 4096:
            continue
        m = _INDEX_LINE.match(line.strip())
        if not m or not m.group("hook"):
            continue
        stem = Path(m.group("path")).stem
        # Cap hook text and strip newlines defensively (the regex anchors at
        # line end so newlines shouldn't appear, but a hook that contains a
        # closing markdown block could still surprise the regenerated index).
        hook = m.group("hook").strip().splitlines()[0][:300]
        hooks[stem] = hook
    return hooks


def write_index(
    target_root: Path,
    written: list[tuple[str, Path]],
    hooks: dict[str, str] | None = None,
    stem_remap: dict[str, str] | None = None,
) -> Path:
    """Rewrite MEMORY.md as a one-line-per-entry index pointing at new locations.

    `hooks` maps source-file stem → human-curated hook text from the source
    MEMORY.md. `stem_remap` maps migrated stem → source stem so hooks keyed
    under the source stem (e.g. `user_alice`) still resolve after the
    basename was rewritten by prefix stripping (e.g. → `alice`).
    """
    hooks = hooks or {}
    stem_remap = stem_remap or {}
    target_path = target_root / "memory" / "MEMORY.md"
    target_path.parent.mkdir(parents=True, exist_ok=True)

    lines = ["# Memory Index", ""]
    by_section: dict[str, list[tuple[str, Path]]] = {
        "Lessons (graduated)": [],
        "Profile": [],
        "Notes": [],
        "References": [],
    }
    for category, path in written:
        if category == "feedback":
            by_section["Lessons (graduated)"].append((category, path))
        elif category == "user":
            by_section["Profile"].append((category, path))
        elif category in ("project", "cycle", "misc"):
            by_section["Notes"].append((category, path))
        elif category == "reference":
            by_section["References"].append((category, path))

    for section, items in by_section.items():
        if not items:
            continue
        lines.append(f"## {section}")
        lines.append("")
        for _, path in sorted(items, key=lambda p: p[1].name):
            rel = path.relative_to(target_root / "memory")
            # Look up the hook under the source stem (the name as it appeared
            # in the source MEMORY.md), falling back to the migrated stem.
            source_stem = stem_remap.get(path.stem, path.stem)
            hook = hooks.get(source_stem) or hooks.get(path.stem)
            suffix = f" — {hook}" if hook else ""
            lines.append(f"- [{path.stem}]({rel}){suffix}")
        lines.append("")

    atomic_write_text(target_path, "\n".join(lines).rstrip() + "\n")
    return target_path


# Source-relative-path prefixes that are already in target shape and should
# be preserved verbatim under <target>/memory/. Order matters: longer/more
# specific prefixes first so `personal/profile/` wins over `personal/`.
_TARGET_SHAPED_PREFIXES = (
    Path("semantic") / "lessons",
    Path("personal") / "profile",
    Path("personal") / "notes",
    Path("personal") / "references",
)


def _is_under(rel: Path, prefix: Path) -> bool:
    """True if `rel` lives under directory `prefix`."""
    try:
        rel.relative_to(prefix)
    except ValueError:
        return False
    return True


def route_file(path: Path, src_root: Path) -> tuple[str, Path, str] | None:
    """Decide the destination category + relative target dir for a source .md.

    Pure function — does not write anything. Returns
    `(category, target_dir_rel, strip_prefix)`:
      - `category` ∈ {"feedback","user","project","cycle","misc","reference"}
      - `target_dir_rel` — path under `<dst>/memory/` (without the filename)
      - `strip_prefix` — string to strip from the basename when writing.
        Empty for already-target-shaped paths (preserves names verbatim).
        `"user_"` / `"reference_"` for legacy flat-prefix routing.
    Returns None for files that should be skipped (e.g. top-level MEMORY.md).

    For nested target-shaped paths (e.g. `semantic/lessons/sub/feedback_x.md`),
    the deeper relative directory is preserved so the migrated companion lands
    at the matching nested location — losslessness contract.

    Per codex review: the explicit `strip_prefix` element is what lets a
    source like `personal/profile/user_alice.md` (already at its target shape)
    round-trip verbatim, instead of getting incorrectly demangled to
    `personal/profile/alice.md` and silently colliding with any sibling
    `alice.md`.
    """
    rel = path.relative_to(src_root)

    # Top-level MEMORY.md — never copied; regenerated as the index.
    if rel == Path("MEMORY.md"):
        return None

    # Nested target-shaped paths: preserve the relative directory verbatim
    # AND preserve the filename verbatim (no prefix stripping). This is what
    # modern Claude / Cursor / Codex auto-memory produces.
    for prefix in _TARGET_SHAPED_PREFIXES:
        if not _is_under(rel, prefix):
            continue
        if prefix == Path("semantic") / "lessons":
            if path.name.startswith("feedback_"):
                return ("feedback", rel.parent, "")
            return ("misc", rel.parent, "")
        if prefix == Path("personal") / "profile":
            return ("user", rel.parent, "")
        if prefix == Path("personal") / "notes":
            return ("misc", rel.parent, "")
        if prefix == Path("personal") / "references":
            return ("reference", rel.parent, "")

    # Flat at root (or under an unrecognized subdir) → prefix-based routing
    # with the legacy strip-prefix behavior so `user_alice.md` lands at
    # `personal/profile/alice.md`.
    cat = categorize(path.name)
    if cat == "feedback":
        return ("feedback", Path("semantic") / "lessons", "")
    if cat == "user":
        return ("user", Path("personal") / "profile", "user_")
    if cat in ("project", "cycle", "misc"):
        return (cat, Path("personal") / "notes", "")
    if cat == "reference":
        return ("reference", Path("personal") / "references", "reference_")
    return None


def main():
    if len(sys.argv) != 3:
        print("usage: migrate.py <source-flat-dir> <target-brain-root>", file=sys.stderr)
        return 2

    src = Path(sys.argv[1]).expanduser().resolve()
    dst = Path(sys.argv[2]).expanduser().resolve()

    if not src.is_dir():
        print(f"migrate: source not a directory: {src}", file=sys.stderr)
        return 2

    # Refuse self-recursion. install.sh's idempotency guard handles the common
    # case (post-install symlink), but a power user invoking migrate.py
    # directly with a symlinked source could otherwise walk the brain itself
    # and re-write its own files mid-iteration. Refuse if dst overlaps src.
    if src == dst or src in dst.parents or dst in src.parents:
        print(
            f"migrate: source ({src}) and target ({dst}) overlap; refusing",
            file=sys.stderr,
        )
        return 2

    dst.mkdir(parents=True, exist_ok=True)

    # Hooks from source MEMORY.md (if any) — preserved into the regenerated
    # index so users don't lose their human-curated one-line descriptions.
    hooks = parse_index_hooks(src / "MEMORY.md")

    lessons: list[dict] = []
    written: list[tuple[str, Path]] = []
    # Map migrated stem → source stem so write_index can look up MEMORY.md
    # hooks even when the basename was rewritten by prefix stripping
    # (`user_alice.md` → `alice.md`). Per codex review: hooks were silently
    # dropped for stripped flat filenames before this map existed.
    stem_remap: dict[str, str] = {}

    src_resolved = src.resolve()

    # Recursive walk: handles both flat (legacy) and nested (modern Claude /
    # Cursor) source layouts. Sorted for deterministic output.
    for path in sorted(src.rglob("*.md")):
        # Skip symlinked files unconditionally (security review #1):
        #   a malicious or accidental symlink in the source could otherwise
        #   pull arbitrary file content (e.g. ~/.ssh/id_rsa) into the brain
        #   and onto the next git push.
        if path.is_symlink():
            continue
        if not path.is_file():
            continue
        # Defense-in-depth: reject paths that resolve outside src (e.g. via
        # a symlinked intermediate dir that rglob descended into on Py 3.13+).
        try:
            if not str(path.resolve()).startswith(str(src_resolved)):
                continue
        except OSError:
            continue
        route = route_file(path, src)
        if route is None:
            continue
        cat, target_rel, strip_prefix = route

        if cat == "feedback":
            # `migrate_feedback` writes the companion AND returns the row.
            # Pass the per-file target_rel so nested `semantic/lessons/sub/`
            # source files preserve their `sub/` segment in the brain.
            lesson = migrate_feedback(path, dst, companion_rel_dir=target_rel)
            lessons.append(lesson)
            companion = dst / "memory" / target_rel / path.name
            written.append(("feedback", companion))
            # Feedback filenames aren't rewritten, so source stem == migrated stem.
            stem_remap[path.stem] = path.stem
            continue

        # Non-feedback: write file verbatim, applying any prefix stripping
        # determined by route_file (only the flat-source layout sets a non-
        # empty `strip_prefix`; target-shaped sources preserve names verbatim).
        target_dir = dst / "memory" / target_rel
        new_path = write_simple_file(path, target_dir, strip_prefix)
        written.append((cat, new_path))
        # Record the source-stem-of-migrated-stem mapping so MEMORY.md hooks
        # keyed under the source stem still resolve after the basename rewrite.
        stem_remap[new_path.stem] = path.stem

    if lessons:
        write_lessons_jsonl(lessons, dst)

    index_path = write_index(dst, written, hooks=hooks, stem_remap=stem_remap)

    print(f"migrate: wrote {len(lessons)} lesson(s), {len(written)} file(s)")
    print(f"         index: {index_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
