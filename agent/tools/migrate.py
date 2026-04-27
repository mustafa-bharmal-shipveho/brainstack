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
import datetime
import hashlib
import json
import os
import re
import sys
from pathlib import Path


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


def lesson_id_from_stem(stem: str) -> str:
    """Stable id derived from source filename — keeps re-migration idempotent."""
    h = hashlib.md5(stem.encode()).hexdigest()[:12]
    return f"lesson_{h}"


def migrate_feedback(src_path: Path, target_root: Path) -> dict:
    """Convert one feedback_*.md file into a lesson row + companion markdown."""
    text = src_path.read_text()
    meta, body = parse_frontmatter(text)
    claim, why, how_to_apply = parse_feedback(body)

    # Companion preserves the original filename (incl. `feedback_` prefix)
    # so audit / round-trip tracing back to the source is unambiguous.
    lid = lesson_id_from_stem(src_path.stem)

    # Write companion markdown verbatim
    companion_dir = target_root / "memory" / "semantic" / "lessons"
    companion_dir.mkdir(parents=True, exist_ok=True)
    companion_path = companion_dir / src_path.name
    companion_path.write_text(text)

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
    body_first_para = claim.split("\n\n", 1)[0].strip()
    claim_text = body_first_para if body_first_para else (meta.get("description") or "").strip()
    return {
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
        "original_markdown_path": f"semantic/lessons/{src_path.name}",
    }


def write_lessons_jsonl(lessons: list[dict], target_root: Path) -> Path:
    semantic_dir = target_root / "memory" / "semantic"
    semantic_dir.mkdir(parents=True, exist_ok=True)
    path = semantic_dir / "lessons.jsonl"
    # Idempotent: rewrite the file from scratch each run, sorted by id
    by_id = {L["id"]: L for L in lessons}
    sorted_lessons = [by_id[k] for k in sorted(by_id)]
    with path.open("w") as f:
        for L in sorted_lessons:
            f.write(json.dumps(L) + "\n")
    return path


def write_simple_file(src_path: Path, target_dir: Path, slug_prefix: str = "") -> Path:
    """Copy file content to target_dir, stripping `slug_prefix` from filename."""
    target_dir.mkdir(parents=True, exist_ok=True)
    new_name = src_path.name
    if slug_prefix and new_name.startswith(slug_prefix):
        new_name = new_name[len(slug_prefix):]
    target_path = target_dir / new_name
    target_path.write_text(src_path.read_text())
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


def write_index(target_root: Path, written: list[tuple[str, Path]]) -> Path:
    """Rewrite MEMORY.md as a one-line-per-entry index pointing at new locations."""
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
            lines.append(f"- [{path.stem}]({rel})")
        lines.append("")

    target_path.write_text("\n".join(lines).rstrip() + "\n")
    return target_path


def main():
    if len(sys.argv) != 3:
        print("usage: migrate.py <source-flat-dir> <target-brain-root>", file=sys.stderr)
        return 2

    src = Path(sys.argv[1]).expanduser().resolve()
    dst = Path(sys.argv[2]).expanduser().resolve()

    if not src.is_dir():
        print(f"migrate: source not a directory: {src}", file=sys.stderr)
        return 2

    dst.mkdir(parents=True, exist_ok=True)

    lessons: list[dict] = []
    written: list[tuple[str, Path]] = []

    for path in sorted(src.iterdir()):
        if not path.is_file() or path.suffix != ".md":
            continue
        cat = categorize(path.name)
        if cat == "index":
            continue
        if cat == "feedback":
            lesson = migrate_feedback(path, dst)
            lessons.append(lesson)
            companion = dst / "memory" / lesson["original_markdown_path"]
            written.append(("feedback", companion))
        elif cat == "user":
            target_dir = dst / "memory" / "personal" / "profile"
            new_path = write_simple_file(path, target_dir, "user_")
            written.append(("user", new_path))
        elif cat in ("project", "cycle", "misc"):
            target_dir = dst / "memory" / "personal" / "notes"
            new_path = write_simple_file(path, target_dir)
            written.append((cat, new_path))
        elif cat == "reference":
            target_dir = dst / "memory" / "personal" / "references"
            new_path = write_simple_file(path, target_dir, "reference_")
            written.append(("reference", new_path))

    if lessons:
        write_lessons_jsonl(lessons, dst)

    index_path = write_index(dst, written)

    print(f"migrate: wrote {len(lessons)} lesson(s), {len(written)} file(s)")
    print(f"         index: {index_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
