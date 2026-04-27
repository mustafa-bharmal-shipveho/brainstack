#!/usr/bin/env python3
"""Context-preserving employer-identity scrubber.

Walks a brain (or any tree of .md / .jsonl files) and replaces specific
employer-identifying terms with role-typed placeholders. The goal is C1
remediation: keep the brain useful (each entry's structure and meaning
stays intact) while removing direct attribution that a personal-account
GitHub remote shouldn't carry.

This is NOT a credential scanner — that's `redact.py`. This is for
*identity* (employer name, colleague names, internal repos, internal
URLs, vault IDs).

Typical use:

    python3 scrub_employer.py ~/.agent --dry-run        # preview
    python3 scrub_employer.py ~/.agent                   # apply
    python3 scrub_employer.py ~/.agent --map mymap.yml   # custom map

Default substitution map is defined in `_default_substitutions()` below.
The map can be overridden with `--map <yaml>`. Invalid YAML or invalid
regex entries are reported and skipped (we never crash mid-run).

Files renamed by the tool (when their slug carries an identifying term)
are reported in the summary so the user can update external references.

Atomic: every file write goes through `_atomic.atomic_write_bytes`.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
_MEMORY_DIR = SCRIPT_DIR.parent / "memory"
if _MEMORY_DIR.is_dir() and str(_MEMORY_DIR) not in sys.path:
    sys.path.insert(0, str(_MEMORY_DIR))
try:
    from _atomic import atomic_write_bytes
except ImportError:
    def atomic_write_bytes(path, data):
        Path(path).write_bytes(data)


def _full_substitutions() -> list[tuple[str, str, str]]:
    """The maximal map — for users moving the brain to a non-employer-tied
    remote. Scrubs employer name, internal URLs, internal repo codenames,
    internal vault references, NotebookLM IDs, colleague names, AND your
    own name.

    These are EXAMPLES. Edit the lists to match your org. The framework
    can't know your specific employer / colleague / repo names; the patterns
    here are placeholders showing the shape of what to substitute.
    """
    return [
        # ---- Example: compound URL / email ----
        # Replace `<your-org>` with your actual employer slug (e.g. "acme").
        # (r"https://internal-code-search\.<your-org>\.com", "https://internal-code-search.example", "internal_url"),
        # (r"@<your-org>\.com", "@example.org", "internal_email_domain"),
        # (r"\b<your-org>\.com\b", "example.org", "internal_domain"),

        # ---- Example: package / org names ----
        # (r"\b<your-org>-technologies\b", "example-technologies", "internal_org"),
        # (r"@<your-org>/", "@example/", "internal_pkg_scope"),

        # ---- 1Password vault references (generic shape) ----
        (r"\bShared - Platform \[[A-Z]\]\b", "<INTERNAL_VAULT>", "internal_vault"),

        # ---- 1Password item IDs (26-char base32 lowercase) ----
        (r"\b[a-z2-7]{26}\b", "<OP_ITEM_ID>", "internal_op_item_id"),

        # ---- Internal NotebookLM URLs (specific notebook IDs) ----
        (
            r"https://notebooklm\.google\.com/notebook/[a-f0-9-]+",
            "<INTERNAL_NOTEBOOK_URL>",
            "internal_notebook_url",
        ),

        # ---- Example: employer name ----
        # (r"\bAcme\b", "Employer", "employer_name"),
        # (r"\bacme\b", "employer", "employer_name"),

        # ---- Example: colleague + self (PII) ----
        # (r"\b<colleague-firstname>\b", "Colleague", "colleague"),
        # (r"\b<your-firstname>\b", "User", "self_name"),
    ]


def _pii_only_substitutions() -> list[tuple[str, str, str]]:
    """The narrower map — for users keeping the brain on an employer-tied
    private remote. Scrubs only third-party PII: colleague names. Employer
    terms / internal repos / your own name all stay.

    These are EXAMPLES. Edit the list to match the colleagues you've
    referenced in your brain. The framework cannot enumerate names for
    you (and a wholesale grep of personal/notes/ is intentionally not
    automated — that's intrusive).
    """
    return [
        # Example shape — uncomment + edit:
        # (r"\bAlice\b", "Colleague", "colleague"),
        # (r"\balice\b", "colleague", "colleague"),
    ]


def _default_substitutions(mode: str = "full") -> list[tuple[re.Pattern, str, str]]:
    raw = _pii_only_substitutions() if mode == "pii-only" else _full_substitutions()
    compiled: list[tuple[re.Pattern, str, str]] = []
    for pat, repl, label in raw:
        try:
            compiled.append((re.compile(pat), repl, label))
        except re.error as e:
            sys.stderr.write(f"scrub-employer: invalid default regex {pat!r}: {e}\n")
    return compiled


def _filename_slug_substitutions(mode: str = "full") -> dict[str, str]:
    """Filename slugs that should be renamed (without extension).

    Only listed slugs are renamed — every other file keeps its name.
    These are EXAMPLES — edit to match slugs in your brain that carry
    identifying terms. The framework ships an empty default; users add
    entries here as they discover them.
    """
    # Example shapes — uncomment + edit:
    # if mode == "pii-only":
    #     return {"<colleague-firstname>_boss": "boss_profile"}
    # return {
    #     "<colleague-firstname>_boss": "boss_profile",
    #     "project_<your-org>_systems_notebook": "project_systems_notebook",
    # }
    return {}


SKIP_DIRS = frozenset({".git", "__pycache__", ".pytest_cache", "node_modules", ".venv"})


def _iter_text_files(root: Path) -> Iterable[Path]:
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix not in (".md", ".jsonl", ".txt", ".json", ".yml", ".yaml"):
            continue
        yield p


def _scrub_text(text: str, subs: list[tuple[re.Pattern, str, str]]) -> tuple[str, dict[str, int]]:
    """Apply all substitutions; return (new_text, hits_by_label)."""
    hits: dict[str, int] = {}
    out = text
    for pat, repl, label in subs:
        new, n = pat.subn(repl, out)
        if n:
            hits[label] = hits.get(label, 0) + n
            out = new
    return out, hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("brain_root", help="Brain dir to scrub (e.g. ~/.agent)")
    ap.add_argument("--dry-run", action="store_true", help="Report changes without writing")
    ap.add_argument(
        "--no-rename",
        action="store_true",
        help="Don't rename files even if their slug is in the rename map",
    )
    ap.add_argument(
        "--mode",
        choices=("full", "pii-only"),
        default="full",
        help=(
            "full: scrub employer + internal + colleague + self (for non-"
            "employer remotes). pii-only: scrub only colleague names "
            "(for employer-tied private remotes). Default: full."
        ),
    )
    args = ap.parse_args()

    root = Path(os.path.expanduser(args.brain_root)).resolve()
    if not root.exists():
        sys.stderr.write(f"scrub-employer: not found: {root}\n")
        return 2

    subs = _default_substitutions(args.mode)
    rename_map = _filename_slug_substitutions(args.mode)

    total_files_changed = 0
    total_hits: dict[str, int] = {}
    rename_pairs: list[tuple[Path, Path]] = []

    # Step 1: scrub contents
    for f in _iter_text_files(root):
        try:
            text = f.read_text()
        except OSError as e:
            sys.stderr.write(f"scrub-employer: cannot read {f}: {e}\n")
            continue
        new_text, hits = _scrub_text(text, subs)
        if hits:
            total_files_changed += 1
            for label, n in hits.items():
                total_hits[label] = total_hits.get(label, 0) + n
            print(f"{f.relative_to(root)}: " + ", ".join(
                f"{label}={n}" for label, n in sorted(hits.items())
            ))
            if not args.dry_run:
                atomic_write_bytes(f, new_text.encode("utf-8"))

    # Step 2: rename files whose slug is in the rename map
    if not args.no_rename:
        for f in list(_iter_text_files(root)):
            slug = f.stem
            if slug in rename_map:
                new_path = f.with_name(rename_map[slug] + f.suffix)
                if new_path.exists():
                    sys.stderr.write(
                        f"scrub-employer: rename target already exists, skipping: {new_path}\n"
                    )
                    continue
                rename_pairs.append((f, new_path))
                if not args.dry_run:
                    f.rename(new_path)

        # Step 3: rewrite MEMORY.md / inline links to renamed files
        memory_md = root / "memory" / "MEMORY.md"
        if memory_md.exists() and rename_pairs:
            text = memory_md.read_text()
            for old, new in rename_pairs:
                # Match `[label](old.md)` and `(old.md)` etc.
                old_name = old.name
                new_name = new.name
                old_stem = old.stem
                new_stem = new.stem
                text = text.replace(old_name, new_name)
                # Slug-only references (e.g. inside markdown link labels)
                text = re.sub(rf"\b{re.escape(old_stem)}\b", new_stem, text)
            if not args.dry_run:
                atomic_write_bytes(memory_md, text.encode("utf-8"))
            print(f"\nUpdated MEMORY.md links: {len(rename_pairs)} renames")

    # Summary
    print()
    print(f"== Scrub summary ==")
    print(f"Files changed:    {total_files_changed}")
    print(f"Substitutions:    {sum(total_hits.values())}")
    for label, n in sorted(total_hits.items(), key=lambda x: -x[1]):
        print(f"  {label:30s} {n}")
    if rename_pairs:
        print(f"Files renamed:    {len(rename_pairs)}")
        for old, new in rename_pairs:
            print(f"  {old.name} -> {new.name}")
    if args.dry_run:
        print("(dry-run — nothing written)")

    return 1 if (total_files_changed or rename_pairs) else 0


if __name__ == "__main__":
    sys.exit(main())
