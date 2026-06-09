"""Deterministic staleness + integrity linting for the brain.

`recall` is retrieval-only and never mutates the brain during a query.
`lint` is a separate maintenance surface: it READS memory files and
REPORTS checkable problems. It writes only when explicitly asked
(``--mark``), and even then only adds a ``needs_review`` frontmatter flag
— it never deletes or rewrites memory content. This keeps the
human-gated promotion model intact: lint surfaces candidates for review,
the human decides.

Design priority is PRECISION over recall. A memory wrongly flagged as
stale erodes trust in the whole pass, so every check here is
deterministic, offline, and conservative. We would rather miss a stale
memory than flag a healthy one.

Checks
------
- ``dead_path``      — an absolute or ``~/`` path referenced *inside
  backticks or a markdown link* that does not exist on disk. The headline
  staleness signal: memories pointing at files/plans/scripts that have
  since moved or been deleted.
- ``broken_wikilink`` — a ``[[target]]`` link whose target resolves to no
  memory in the brain.
- ``broken_local_link`` — a ``[text](path)`` markdown link to a local
  file that does not exist.
- ``missing_frontmatter`` — a lesson missing ``name`` / ``description`` /
  ``type``.

Deliberately NOT checked (to keep false positives near zero)
------------------------------------------------------------
- Repo-relative paths (``src/foo.py``) — ambiguous without a repo root.
- Bare paths in prose (not backticked) — casual mentions, high FP rate.
- URL liveness — needs network; off the offline-deterministic path.
- Semantic contradictions between memories — noisy; out of scope for v1.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from recall.frontmatter import parse_path

# Staleness checks (gated behind --stale) vs integrity checks.
STALE_KINDS = frozenset({"dead_path", "broken_wikilink", "broken_local_link"})
INTEGRITY_KINDS = frozenset({"missing_frontmatter", "unparseable_frontmatter"})
ALL_KINDS = STALE_KINDS | INTEGRITY_KINDS

# A path candidate must begin with one of these to even be *considered* an
# absolute/home path. Repo-relative paths are intentionally excluded (too
# ambiguous to check without a repo root). Passing this is necessary but
# not sufficient — see `_is_checkable_fs_path` for the real filesystem-root
# gate that distinguishes files from URL routes / slash-commands.
_PATH_PREFIXES = ("/", "~/", "$HOME/", "~\\")

# After expansion, a candidate is only existence-checked if it lives under a
# real, durable filesystem root. This is what separates an actual file path
# (`/Users/me/x.md`) from a slash-command (`/agent-team`), a URL route
# (`/login`, `/noc/intake`), or an API endpoint — none of which start with a
# real root, so none get flagged as "dead files".
_CHECKABLE_ROOTS = (
    "/Users/", "/home/", "/root/", "/opt/", "/etc/", "/usr/", "/bin/",
    "/sbin/", "/Applications/", "/Library/", "/srv/", "/mnt/", "/data/",
    "/Volumes/", "/private/etc/", "/private/var/db/",
)

# Ephemeral roots: paths here are SUPPOSED to disappear, so a memory
# referencing one is never a durable-staleness signal — flagging them is
# pure noise. Excluded even though they're under "/".
_EPHEMERAL_ROOTS = (
    "/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/",
    "/var/tmp/",
)

# Trailing line/column reference appended to a path (editor / grep style):
# `:12`, `:12,34` (multi-line), or `:12:34` (line:col). Not part of the
# path; strip before existence check.
_LINE_REF_RE = re.compile(r":\d+(?:[:,]\d+)*$")

# Substrings that mark a candidate as an illustrative placeholder, not a
# real path. Conservative on purpose — when in doubt, skip (don't flag).
_PLACEHOLDER_MARKERS = (
    "<", ">", "*", "{", "}", "...", "/path/to", "/path", "your-", "<you",
    "XXX", "example.com", "$1", "%s", "{}", "…",
    # Conventional placeholder home dirs — never a real path on this machine.
    "/Users/me/", "/Users/you/", "/Users/user/", "/Users/username/",
    "/Users/name/", "/Users/<", "/home/user/", "/home/you/",
)

# Brain-native file types. Relative markdown links are only existence-checked
# when they point at one of these AND resolve inside the brain — a relative
# link to `src/foo.ts` is a code-repo path, unverifiable without the repo, so
# it is intentionally skipped (see _check_paths_and_links).
_BRAIN_CONTENT_EXTS = frozenset({
    ".md", ".markdown", ".canvas", ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".pdf", ".webp", ".txt",
})

# Backtick spans: `...`. Non-greedy, single-line.
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")

# Markdown link: [text](target). Target captured.
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")

# Wikilink: [[target]] or [[target|alias]].
_WIKILINK_RE = re.compile(r"\[\[([^\]|\n]+)(?:\|[^\]\n]+)?\]\]")

# Abs/home path token embedded in a larger span (e.g. `cat /Users/x/y.md`).
_EMBEDDED_PATH_RE = re.compile(
    r"(?:~|\$HOME|/Users/|/home/|/opt/|/etc/|/var/|/tmp/|/usr/)[^\s`'\"]*"
)

# Trailing characters to strip from an extracted path (sentence punctuation,
# closing brackets/quotes). We do NOT strip a trailing '.' because filenames
# legitimately end in extensions; backticked paths rarely carry a trailing
# period anyway.
_TRAILING_STRIP = ")],;:'\""


@dataclass(frozen=True)
class Finding:
    """One lint problem in one memory file."""

    file: Path
    line: int
    kind: str
    severity: str  # "stale" | "integrity"
    detail: str
    evidence: str

    def to_dict(self) -> dict:
        return {
            "file": str(self.file),
            "line": self.line,
            "kind": self.kind,
            "severity": self.severity,
            "detail": self.detail,
            "evidence": self.evidence,
        }


def _expand(path_str: str) -> str:
    """Expand ~ and $HOME / env vars in a path string."""
    return os.path.expanduser(os.path.expandvars(path_str))


def _safe_exists(path_str: str) -> bool:
    """`Path.exists()` that never raises (null bytes, OS errors, loops)."""
    try:
        return Path(path_str).exists()
    except (OSError, ValueError, RuntimeError):
        return False


def _is_placeholder(candidate: str) -> bool:
    return any(marker in candidate for marker in _PLACEHOLDER_MARKERS)


def _looks_like_abs_or_home(candidate: str) -> bool:
    return candidate.startswith(_PATH_PREFIXES)


def _strip_line_ref(candidate: str) -> str:
    """Remove a trailing ``:12`` / ``:12,34`` line-reference suffix."""
    return _LINE_REF_RE.sub("", candidate)


def _is_checkable_fs_path(resolved: str) -> bool:
    """True if an expanded path is a real filesystem path worth checking.

    Excludes URL routes / slash-commands (not under a real root) and
    ephemeral temp dirs (their absence is expected, not a staleness signal).
    """
    if not resolved.startswith("/"):
        return False
    if resolved.startswith(_EPHEMERAL_ROOTS):
        return False
    return resolved.startswith(_CHECKABLE_ROOTS)


def _path_is_dead(candidate: str) -> bool:
    """Decide whether a path candidate is a genuine dead reference.

    Conservative: returns False (not dead) unless we find a *checkable*
    filesystem path that does not exist. Handles three real-brain shapes:

      - ``foo.md:3,7``               → strip line-ref, then check
      - ``~/go/bin/nlm auth "x"``    → command; first token is the real path
      - ``/Users/me/space path.md``  → whole span is the path (has spaces)
    """
    whole = _strip_line_ref(candidate)
    variants = [whole]
    if " " in candidate:
        variants.append(_strip_line_ref(candidate.split()[0]))

    checkable = [(v, _expand(v)) for v in variants if _is_checkable_fs_path(_expand(v))]
    if not checkable:
        return False  # route / command / ephemeral — nothing to verify
    if any(_safe_exists(resolved) for _, resolved in checkable):
        return False  # at least one real path resolves — live reference
    return True


def _strip_trailing(candidate: str) -> str:
    return candidate.rstrip(_TRAILING_STRIP).rstrip()


def _strip_trailing_annotation(span: str) -> str:
    """Strip a single trailing ``(...)`` annotation from a path span.

    Real-brain paths are often written like::

        `/Users/me/.claude/plans/foo.md (6-step plan, not executed)`

    The parenthetical is prose, not part of the path. Strip exactly one
    trailing balanced-ish ``(...)`` group plus surrounding space.
    """
    return re.sub(r"\s*\([^)]*\)\s*$", "", span).strip()


_QUOTED_RE = re.compile(r'"([^"]*)"|\'([^\']*)\'')


def _path_candidates_in_span(span: str) -> list[str]:
    """Extract path candidates from one backtick span.

    Strategy (precision-first):
      - If the *whole* span is a path (starts with an abs/home marker),
        treat the whole cleaned span as THE candidate. This correctly
        handles paths containing spaces (e.g. ``.../SVP Roadmap.md``) and
        avoids truncating them mid-path, which would be a false positive.
      - Otherwise the span is a command or prose. Pull out quoted segments
        first (``cat "/Users/x/My Notes/p.md"`` → the whole quoted path,
        spaces and all), then scan the un-quoted remainder for bare path
        tokens (``cat /Users/x/y.md``).
    """
    cleaned = _strip_trailing(_strip_trailing_annotation(span.strip()))
    if _looks_like_abs_or_home(cleaned):
        return [cleaned]

    candidates: list[str] = []

    # Quoted segments — capture spaced paths without truncation, and blank
    # them out of the remainder so the bare-token scan can't re-truncate them.
    def _blank(m: re.Match[str]) -> str:
        seg = m.group(1) if m.group(1) is not None else m.group(2)
        seg = _strip_trailing(seg.strip())
        if _looks_like_abs_or_home(seg):
            # The whole quoted segment is a path (possibly with spaces).
            # Consume it AND blank it from the remainder so the bare-token
            # scan can't re-truncate it.
            candidates.append(seg)
            return " " * len(m.group(0))
        # Quoted prose that merely *contains* a path (e.g. "see /x/y.md").
        # Leave it in place so the bare-token scan can still find the path —
        # blanking it would hide a genuinely dead reference.
        return m.group(0)

    remainder = _QUOTED_RE.sub(_blank, span)
    candidates.extend(_strip_trailing(m.group(0)) for m in _EMBEDDED_PATH_RE.finditer(remainder))

    # Dedup, preserve order.
    seen: set[str] = set()
    return [c for c in candidates if c and not (c in seen or seen.add(c))]


def _slugify(name: str) -> str:
    """Normalize a wikilink target / memory name for matching."""
    name = unicodedata.normalize("NFC", name).strip().lower()
    name = re.sub(r"[\s]+", "-", name)
    name = re.sub(r"[^a-z0-9_\-./]", "", name)
    return name.strip("-/")


def _wikilink_target_key(target: str) -> str:
    """Slugify a wikilink target, ignoring an Obsidian #heading / ^block
    fragment and a redundant trailing extension. ``[[Note#Heading]]`` and
    ``[[Note.md]]`` both resolve to the same memory as ``[[Note]]``.
    """
    target = re.split(r"[#^]", target, maxsplit=1)[0].strip()
    target = re.sub(r"\.(md|markdown)$", "", target, flags=re.IGNORECASE)
    return _slugify(target)


def _known_memory_keys(brain_root: Path) -> set[str]:
    """Build the set of resolvable wikilink targets in the brain.

    A ``[[target]]`` is considered resolvable if ``target`` (slugified)
    matches any memory's filename stem OR its frontmatter ``name``.
    """
    keys: set[str] = set()
    for md in brain_root.rglob("*.md"):
        keys.add(_slugify(md.stem))
        try:
            fm = parse_path(md).frontmatter
        except Exception:
            fm = {}
        name = fm.get("name")
        if isinstance(name, str) and name.strip():
            keys.add(_slugify(name))
    return keys


def _iter_lines(text: str) -> Iterable[tuple[int, str]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    yield from enumerate(normalized, start=1)


def _check_paths_and_links(file: Path, text: str, brain_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for lineno, line in _iter_lines(text):
        seen_on_line: set[str] = set()

        # Dead path refs inside backtick spans.
        for m in _BACKTICK_RE.finditer(line):
            for cand in _path_candidates_in_span(m.group(1)):
                if not cand or not _looks_like_abs_or_home(cand):
                    continue
                if _is_placeholder(cand):
                    continue
                key = _strip_line_ref(cand)
                if key in seen_on_line:
                    continue
                seen_on_line.add(key)
                if _path_is_dead(cand):
                    findings.append(Finding(
                        file=file, line=lineno, kind="dead_path", severity="stale",
                        detail=f"referenced path does not exist: {key}",
                        evidence=m.group(0),
                    ))

        # Broken local markdown links.
        for m in _MD_LINK_RE.finditer(line):
            target = m.group(1).strip()
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            # Drop a trailing #fragment (heading/line anchor) and percent-decode
            # %20-style escapes so `./My%20Plan.md` matches `My Plan.md`.
            target = target.split("#", 1)[0]
            if "%" in target:
                from urllib.parse import unquote
                target = unquote(target)
            target = target.strip()
            if not target or _is_placeholder(target):
                continue

            if _looks_like_abs_or_home(target):
                # Absolute/home link target: same rules as a backticked path.
                key = _strip_line_ref(target)
                if key in seen_on_line:
                    continue
                seen_on_line.add(key)
                if _path_is_dead(target):
                    findings.append(Finding(
                        file=file, line=lineno, kind="broken_local_link", severity="stale",
                        detail=f"local link target does not exist: {key}",
                        evidence=m.group(0),
                    ))
                continue

            # Relative link. Only verify brain-native content (.md, images,
            # …) that resolves INSIDE the brain. A relative `src/foo.ts` is a
            # code-repo path — unverifiable without the repo — so skip it.
            if Path(target).suffix.lower() not in _BRAIN_CONTENT_EXTS:
                continue
            try:
                resolved = (file.parent / target).resolve()
                resolved.relative_to(brain_root.resolve())
            except (ValueError, OSError, RuntimeError):
                continue  # outside the brain, or unresolvable — not ours to verify
            if resolved.as_posix() in seen_on_line:
                continue
            seen_on_line.add(resolved.as_posix())
            if not _safe_exists(str(resolved)):
                findings.append(Finding(
                    file=file, line=lineno, kind="broken_local_link", severity="stale",
                    detail=f"local link target does not exist: {target}",
                    evidence=m.group(0),
                ))
    return findings


def _check_wikilinks(file: Path, text: str, known_keys: set[str]) -> list[Finding]:
    findings: list[Finding] = []
    for lineno, line in _iter_lines(text):
        for m in _WIKILINK_RE.finditer(line):
            target = m.group(1).strip()
            if not target or _is_placeholder(target):
                continue
            # Resolve via the fragment-stripped key (``[[Note#Heading]]`` →
            # ``note``) OR the full slug (``[[C# notes]]`` → ``c-notes``, where
            # '#' is just part of the name). Accepting either avoids both the
            # heading-anchor false positive and the '#'-in-name regression.
            if (
                _wikilink_target_key(target) not in known_keys
                and _slugify(target) not in known_keys
            ):
                findings.append(Finding(
                    file=file, line=lineno, kind="broken_wikilink", severity="stale",
                    detail=f"wikilink target resolves to no memory: [[{target}]]",
                    evidence=m.group(0),
                ))
    return findings


_LESSON_REQUIRED_FIELDS = ("name", "description", "type")


def _check_frontmatter(file: Path, frontmatter: dict) -> list[Finding]:
    """Lessons must carry name / description / type frontmatter."""
    if "lessons" not in file.parts:
        return []
    missing = [
        f for f in _LESSON_REQUIRED_FIELDS
        if not (isinstance(frontmatter.get(f), str) and frontmatter[f].strip())
    ]
    if not missing:
        return []
    return [Finding(
        file=file, line=1, kind="missing_frontmatter", severity="integrity",
        detail=f"lesson missing required frontmatter: {', '.join(missing)}",
        # YAML keys can be non-string (e.g. `1:` parses to int); sort by str.
        evidence=f"present: {sorted(map(str, frontmatter.keys()))}",
    )]


def _frontmatter_block_parses(fm_text: str) -> bool:
    """True if a frontmatter block's YAML loads to a mapping (or is empty).

    Empty/whitespace blocks count as parseable (nothing to break); a block
    that yaml rejects, or that loads to a non-mapping, is unparseable.
    """
    import yaml

    if not fm_text.strip():
        return True
    try:
        loaded = yaml.safe_load(fm_text)
    except yaml.YAMLError:
        return False
    return loaded is None or isinstance(loaded, dict)


def _check_unparseable_frontmatter(file: Path) -> list[Finding]:
    """Flag files that HAVE a frontmatter block whose YAML won't parse.

    The common cause is an unquoted scalar value containing a colon (e.g.
    ``outcome: Scope negotiated: 502 ...``). Such a file silently loses ALL
    its frontmatter (type, tags, needs_review) at index time. `recall lint
    --repair` can usually fix these in place.
    """
    bounds = _read_frontmatter_bounds(file)
    if bounds is None:
        return []  # no frontmatter block — nothing to parse
    raw, _newline, body_start, fm_end = bounds
    fm_text = raw[body_start:fm_end]
    if _frontmatter_block_parses(fm_text):
        return []
    return [Finding(
        file=file, line=1, kind="unparseable_frontmatter", severity="integrity",
        detail="frontmatter YAML does not parse (often an unquoted value with a colon); "
               "run `recall lint --repair`",
        evidence=fm_text.strip().splitlines()[0] if fm_text.strip() else "",
    )]


def lint_file(
    file: Path, *, known_keys: set[str], kinds: frozenset[str], brain_root: Path
) -> list[Finding]:
    """Lint one memory file. Never raises — unreadable files yield []."""
    try:
        parsed = parse_path(file)
    except Exception:
        return []
    text = parsed.body
    findings: list[Finding] = []
    # Each check is guarded independently: a crash in one (on pathological
    # input) must not lose the findings from the others, and lint_file must
    # never raise — it runs over arbitrary user markdown.
    if "dead_path" in kinds or "broken_local_link" in kinds:
        try:
            findings.extend(
                f for f in _check_paths_and_links(file, text, brain_root) if f.kind in kinds
            )
        except Exception:  # pragma: no cover - defensive
            pass
    if "broken_wikilink" in kinds:
        try:
            findings.extend(_check_wikilinks(file, text, known_keys))
        except Exception:  # pragma: no cover - defensive
            pass
    if "missing_frontmatter" in kinds:
        try:
            findings.extend(_check_frontmatter(file, parsed.frontmatter))
        except Exception:  # pragma: no cover - defensive
            pass
    if "unparseable_frontmatter" in kinds:
        try:
            findings.extend(_check_unparseable_frontmatter(file))
        except Exception:  # pragma: no cover - defensive
            pass
    return findings


def lint_brain(
    brain_root: Path,
    *,
    kinds: frozenset[str] | None = None,
) -> list[Finding]:
    """Lint every markdown memory under ``brain_root``.

    Returns findings sorted by (file, line, kind) for stable output.
    """
    kinds = kinds or ALL_KINDS
    need_keys = "broken_wikilink" in kinds
    known_keys = _known_memory_keys(brain_root) if need_keys else set()

    findings: list[Finding] = []
    for md in sorted(brain_root.rglob("*.md")):
        findings.extend(lint_file(md, known_keys=known_keys, kinds=kinds, brain_root=brain_root))
    findings.sort(key=lambda f: (str(f.file), f.line, f.kind))
    return findings


def _atomic_write(file: Path, new_raw: str) -> bool:
    """Write ``new_raw`` to ``file`` atomically and symlink-safely.

    mkstemp creates a fresh O_EXCL file in the same dir (so it never
    follows a planted temp symlink), which we then rename over the
    original. Returns True on success.
    """
    import tempfile

    tmpname = None
    try:
        fd, tmpname = tempfile.mkstemp(dir=str(file.parent), prefix=f".{file.name}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(new_raw)
        os.replace(tmpname, file)
        return True
    except (OSError, ValueError):
        if tmpname is not None:
            try:
                os.unlink(tmpname)
            except OSError:
                pass
        return False


def _read_frontmatter_bounds(file: Path) -> tuple[str, str, int, int] | None:
    """Return (raw, newline, body_start, fm_end) for a file with a real
    frontmatter block, or None. fm_end is the index of the newline before
    the closing ``---``. Reads bytes (not read_text) to preserve newline
    style; skips symlinks and non-UTF-8 files.
    """
    try:
        if file.is_symlink():
            return None
        raw = file.read_bytes().decode("utf-8")
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    open_m = re.match(r"---(\r\n|\r|\n)", raw)
    if not open_m:
        return None
    body_start = open_m.end()
    close_m = re.search(r"(?:\r\n|\r|\n)---[ \t]*(?=\r\n|\r|\n|$)", raw[body_start:])
    if not close_m:
        return None
    return raw, open_m.group(1), body_start, body_start + close_m.start()


_NEEDS_REVIEW_TRUE_RE = re.compile(
    r"""(?mi)^needs_review[ \t]*:[ \t]*['"]?(true|yes|1)\b""")


def find_flagged_files(brain_root: Path) -> list[Path]:
    """Every memory under ``brain_root`` whose frontmatter carries a truthy
    top-level ``needs_review`` flag.

    Uses a tolerant regex scan of the raw frontmatter block rather than a
    full YAML parse: some real digests have malformed frontmatter (e.g. an
    unquoted ``outcome:`` value containing a colon) that ``yaml.safe_load``
    rejects, which would otherwise hide their flag. mark/unmark are
    regex-based too, so this keeps detection consistent with mutation.
    """
    flagged: list[Path] = []
    for md in sorted(brain_root.rglob("*.md")):
        bounds = _read_frontmatter_bounds(md)
        if bounds is None:
            continue
        raw, _newline, body_start, fm_end = bounds
        if _NEEDS_REVIEW_TRUE_RE.search(raw[body_start:fm_end]):
            flagged.append(md)
    return flagged


def unmark_needs_review(files: Iterable[Path]) -> list[Path]:
    """Remove a top-level ``needs_review`` line from each file's frontmatter.

    The inverse of mark_needs_review and the mechanism behind auto-clear:
    when a previously-flagged memory no longer has any lint findings (e.g.
    the dead path it referenced was recreated), the stale flag is removed.
    Same safety contract as mark: symlink-skip, UTF-8 only, body bytes and
    newline style preserved exactly, atomic write. Returns files modified.
    """
    modified: list[Path] = []
    for file in files:
        bounds = _read_frontmatter_bounds(file)
        if bounds is None:
            continue
        raw, newline, body_start, fm_end = bounds
        fm_region = raw[body_start:fm_end]
        if not re.search(r"(?m)^needs_review[ \t]*:", fm_region):
            continue  # nothing to remove
        # Drop only the top-level needs_review line(s) from the frontmatter
        # region; body (raw[fm_end:]) is preserved byte-for-byte.
        kept = [
            ln for ln in fm_region.split(newline)
            if not re.match(r"needs_review[ \t]*:", ln)
        ]
        new_raw = raw[:body_start] + newline.join(kept) + raw[fm_end:]
        if _atomic_write(file, new_raw):
            modified.append(file)
    return modified


def mark_needs_review(files: Iterable[Path]) -> list[Path]:
    """Add ``needs_review: true`` to the frontmatter of each file.

    This is the ONLY code path that writes to the brain, so it is
    deliberately conservative:

      - Skips symlinks (never mutate a target outside the brain).
      - Skips files that aren't valid UTF-8, or that lack a real
        frontmatter block (never fabricates one).
      - Splices the flag into the frontmatter block ONLY — body bytes and
        the file's original newline style are preserved exactly (no
        whole-file CRLF normalization).
      - Writes atomically (temp file + ``os.replace``) so a failure can't
        leave a half-written memory.
      - Idempotent: a top-level ``needs_review`` key is left untouched.

    Returns the list of files actually modified.
    """
    modified: list[Path] = []
    for file in files:
        bounds = _read_frontmatter_bounds(file)
        if bounds is None:
            continue  # no frontmatter block / symlink / non-UTF-8 — skip
        raw, newline, body_start, fm_end = bounds
        fm_region = raw[body_start:fm_end]
        # Idempotent: only a TOP-LEVEL needs_review counts (indented keys are
        # nested values, not the flag).
        if re.search(r"(?m)^needs_review[ \t]*:", fm_region):
            continue

        # Splice the flag in just before the closing delimiter. We never
        # split/rejoin the whole file, so body bytes and every existing
        # newline are preserved exactly.
        new_raw = raw[:fm_end] + newline + "needs_review: true" + raw[fm_end:]
        if _atomic_write(file, new_raw):
            modified.append(file)
    return modified


# A top-level scalar frontmatter line: `key: value` (value present, not a
# list/map/multiline indicator). Used by the repair pass.
_SCALAR_FM_LINE_RE = re.compile(r"^([A-Za-z_][\w\-]*):[ \t]+(\S.*?)[ \t]*$")


def _value_breaks_bare_yaml(value: str) -> bool:
    """True if an unquoted scalar value would be invalid (or mis-parsed)
    bare YAML: a ``: `` (mapping indicator), a trailing ``:``, a ``  #``
    (comment), or a leading reserved indicator char (e.g. ``@``, `` ` ``)."""
    return (
        ": " in value
        or value.endswith(":")
        or " #" in value
        or value[:1] in "@`!&*?|>%#"
    )


def _double_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _repair_frontmatter_text(fm_text: str, newline: str) -> str | None:
    """Re-quote scalar values that break bare YAML; return repaired text
    only if it now parses to a mapping, else None (never make it worse).

    Conservative: only touches ``key: value`` lines whose value is unquoted
    and contains a YAML-breaking sequence. Lists, maps, already-quoted
    values, comments, and continuation lines are left untouched.
    """
    out: list[str] = []
    changed = False
    for ln in fm_text.split(newline):
        m = _SCALAR_FM_LINE_RE.match(ln)
        if m:
            key, value = m.group(1), m.group(2)
            already_structured = value[:1] in {'"', "'", "[", "{", "|", ">", "&", "*", "#"}
            if not already_structured and _value_breaks_bare_yaml(value):
                out.append(f"{key}: {_double_quote(value)}")
                changed = True
                continue
        out.append(ln)
    if not changed:
        return None
    repaired = newline.join(out)
    if not _frontmatter_block_parses(repaired):
        return None  # couldn't safely fix — leave the original for manual review
    return repaired


def repair_frontmatter(files: Iterable[Path], *, dry_run: bool = False) -> list[Path]:
    """Repair files whose frontmatter YAML doesn't parse, in place.

    For each file with an unparseable frontmatter block, re-quote the
    offending scalar value(s) and write atomically — body bytes and newline
    style preserved exactly. Skips files that already parse and files the
    repair can't make parseable (those need a human).

    With ``dry_run=True`` nothing is written; returns the files that WOULD be
    repaired (so callers can preview before mutating the user's notes).
    Returns files fixed (or, in dry-run, fixable).
    """
    fixed: list[Path] = []
    for file in files:
        bounds = _read_frontmatter_bounds(file)
        if bounds is None:
            continue
        raw, newline, body_start, fm_end = bounds
        fm_text = raw[body_start:fm_end]
        if _frontmatter_block_parses(fm_text):
            continue  # already fine
        repaired = _repair_frontmatter_text(fm_text, newline)
        if repaired is None:
            continue
        if dry_run:
            fixed.append(file)
            continue
        new_raw = raw[:body_start] + repaired + raw[fm_end:]
        if _atomic_write(file, new_raw):
            fixed.append(file)
    return fixed
