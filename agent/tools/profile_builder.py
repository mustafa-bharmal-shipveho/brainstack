"""PROFILE.md auto-builder (Phase 2a).

Reads every digest under `memory/semantic/digests/`, asks the configured
LLM provider to roll them up into a structured user profile, and writes
`memory/semantic/PROFILE.md` with YAML front-matter + sections.

What PROFILE.md is for:
  - Answer "what does this person work on?" without re-reading every
    digest.
  - Indexed by recall as a normal markdown doc: `recall profile`
    surfaces it.
  - Distinct from a hand-curated CLAUDE.md (workflow preferences).
    PROFILE.md tracks WHAT you work on, not HOW you want to work.

Idempotency: a content-SHA over the sorted (filename, sha256) of every
digest is stored in PROFILE.md's front-matter as `digest_set_sha`. A
second build with no digest changes is a complete no-op (no LLM call,
no rewrite). Adding/changing/removing a digest invalidates the SHA and
forces a rebuild.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Callable

_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent))
sys.path.insert(0, str(_THIS.parent.parent / "memory"))

from _atomic import atomic_write_text  # type: ignore
from llm_providers import LLMProvider, resolve_provider  # type: ignore
from llm_providers.base import LLMError  # type: ignore


# Framework-pure: do NOT mention any company, stack, tool, or product.
# Domains come from the digests themselves.
SYSTEM_PROMPT = """You build a structured profile of a user by reading per-session digests they've produced over time. Your output drives a long-term recall surface — when the user asks "what do I usually work on?" or "what was that recurring issue?", they'll be shown this profile.

Rules:
  - Output ONLY a JSON object matching the required schema. No prose.
  - All fields derived from the digests provided. Do NOT invent domains, tools, or companies not present in the inputs.
  - Be concise: this is a recall hint, not an essay.
  - Sections:
      domains[]: each {tag, count, last_seen, top_digests[]}. Rank by
        count DESC, then last_seen DESC. Keep top 15.
      active_threads[]: digests in the last 14 days whose outcome is
        "in-progress", "blocked", or "abandoned". Each: {session_id,
        title, outcome, last_seen}. Keep at most 10.
      recent_learnings[]: 1-line distillations from `what_was_learned`
        fields of the last 7 days of digests. Keep at most 10.
      long_running_themes[]: themes that appear across ≥5 digests over
        ≥30 days. Each: {theme, session_count, first_seen}.
"""


PROFILE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "domains": {"type": "array", "items": {"type": "object"}},
        "active_threads": {"type": "array",
                           "items": {"type": "object"}},
        "recent_learnings": {"type": "array",
                             "items": {"type": "string"}},
        "long_running_themes": {"type": "array",
                                "items": {"type": "object"}},
    },
    "required": [
        "domains", "active_threads",
        "recent_learnings", "long_running_themes",
    ],
}


# ---------------------------------------------------------------------------
# Digest parsing — minimal YAML front-matter reader (no PyYAML dep)
# ---------------------------------------------------------------------------

_FRONT_MATTER_RE = re.compile(
    r"^---\n(.*?)\n---\n(.*)$", re.DOTALL,
)


def _parse_simple_yaml_front(body: str) -> tuple[dict, str]:
    """Return ({key: str | list[str]}, rest_of_markdown). Tolerates
    common shapes (flow scalar/array AND block-style multi-line list):

        key: value
        key: "quoted value with spaces"
        key: [a, b, c]
        key: ["a", "b"]
        key:
          - item-a
          - item-b
          - "item with spaces"

    Anything more exotic falls back to the raw string — the profile
    builder doesn't need full YAML, just the digest's tag list +
    dates. Defends against the LLM emitting block-style array syntax
    that the original flow-only parser silently dropped to '' (which
    then crashed downstream consumers expecting a list)."""
    m = _FRONT_MATTER_RE.match(body)
    if not m:
        return {}, body
    front, rest = m.group(1), m.group(2)
    out: dict = {}
    lines = front.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        if ":" not in stripped:
            i += 1
            continue
        k, v = stripped.split(":", 1)
        k = k.strip()
        v = v.strip()
        if not k:
            i += 1
            continue
        if v == "":
            # Possible block-style: peek at indented `- item` lines
            # following this key. Consumes them.
            items: list[str] = []
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if not nxt.strip():
                    break
                if not nxt.startswith(" ") and not nxt.startswith("\t"):
                    break
                item = nxt.lstrip(" \t")
                if not item.startswith("- "):
                    break
                val = item[2:].strip()
                if val.startswith('"') and val.endswith('"'):
                    val = val[1:-1]
                elif val.startswith("'") and val.endswith("'"):
                    val = val[1:-1]
                items.append(val)
                j += 1
            if items:
                out[k] = items
                i = j
                continue
            out[k] = ""
            i += 1
            continue
        if v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            if not inner:
                out[k] = []
            else:
                parts = [p.strip() for p in inner.split(",")]
                out[k] = [p[1:-1] if (p.startswith('"') and p.endswith('"'))
                          else (p[1:-1] if (p.startswith("'")
                                            and p.endswith("'")) else p)
                          for p in parts if p]
        elif v.startswith('"') and v.endswith('"'):
            try:
                out[k] = json.loads(v)
            except json.JSONDecodeError:
                out[k] = v[1:-1]
        else:
            out[k] = v
        i += 1
    return out, rest


def _iter_digests(md_dir: Path):
    """Yield (path, front_matter_dict, body_text) for each digest md.
    Silently skips files that can't be parsed (the profile build is a
    best-effort summary — one malformed file shouldn't halt it)."""
    if not md_dir.is_dir():
        return
    for path in sorted(md_dir.glob("*.md")):
        try:
            raw = path.read_text()
            front, body = _parse_simple_yaml_front(raw)
        except (OSError, ValueError):
            continue
        # Skip explicit archived flag for future-proofing (Phase 2e
        # decay marks old digests `archived: true`). Coerce to string
        # since the LLM may emit anything; never raise on shape.
        archived = front.get("archived", "")
        if not isinstance(archived, str):
            archived = str(archived)
        if archived.lower() in ("true", "yes", "1"):
            continue
        yield path, front, body


# ---------------------------------------------------------------------------
# Digest-set SHA — idempotency key
# ---------------------------------------------------------------------------

def _digest_set_sha(md_dir: Path) -> str:
    """SHA over the sorted (filename, file-sha256) of every digest md.
    Stable across runs, sensitive to ANY change in the digest set."""
    h = hashlib.sha256()
    if not md_dir.is_dir():
        return h.hexdigest()
    for path in sorted(md_dir.glob("*.md")):
        try:
            content = path.read_bytes()
        except OSError:
            continue
        h.update(path.name.encode())
        h.update(b"\0")
        h.update(hashlib.sha256(content).hexdigest().encode())
        h.update(b"\0")
    return h.hexdigest()


def _existing_profile_sha(profile_path: Path) -> str | None:
    """Read the `digest_set_sha` field from the existing PROFILE.md's
    front-matter, if any. None when the file doesn't exist OR the
    field is absent."""
    if not profile_path.is_file():
        return None
    try:
        raw = profile_path.read_text()
    except OSError:
        return None
    front, _ = _parse_simple_yaml_front(raw)
    sha = front.get("digest_set_sha")
    return sha if isinstance(sha, str) and sha else None


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

def _build_prompt(digests: list[tuple[Path, dict, str]]) -> str:
    """Serialize digests into a structured block the LLM can consume.
    Includes title + tags + outcome + dates + what_was_learned only —
    the full body would blow context budget."""
    lines = [f"You have {len(digests)} session digest(s)."]
    for _path, front, body in digests:
        sid = front.get("session_id", "")
        date = str(front.get("started_at", ""))[:10]
        outcome = front.get("outcome", "unknown")
        salience = front.get("salience", "")
        tags = front.get("domain_tags") or []
        if not isinstance(tags, list):
            tags = []
        # Pull title + what_was_learned out of the markdown body
        title_m = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        title = title_m.group(1).strip() if title_m else ""
        learned_m = re.search(
            r"## What was learned\s*\n\s*(.+?)(?=\n##|\Z)",
            body, re.DOTALL,
        )
        learned = learned_m.group(1).strip() if learned_m else ""
        lines.append(
            f"\n---\n"
            f"session_id: {sid}\n"
            f"date: {date}\n"
            f"title: {title}\n"
            f"tags: {tags}\n"
            f"outcome: {outcome}\n"
            f"salience: {salience}\n"
            f"learned: {learned[:300]}\n"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def _render_profile_md(profile: dict, digest_set_sha: str) -> str:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    front = (
        "---\n"
        f"generated_at: {now}\n"
        f"digest_set_sha: {digest_set_sha}\n"
        "---\n\n"
    )
    body_parts = [front, "# User profile\n\n"]
    body_parts.append("## Domains\n\n")
    domains = profile.get("domains") or []
    if domains:
        for d in domains:
            tag = d.get("tag", "?")
            count = d.get("count", "")
            last = d.get("last_seen", "")
            tops = d.get("top_digests") or []
            top_str = ", ".join(str(t) for t in tops[:3])
            body_parts.append(
                f"- **{tag}** ({count}, last seen {last}) — {top_str}\n"
            )
    else:
        body_parts.append("_(none yet)_\n")
    body_parts.append("\n## Active threads\n\n")
    threads = profile.get("active_threads") or []
    if threads:
        for t in threads:
            body_parts.append(
                f"- `{t.get('session_id', '')}` — "
                f"**{t.get('title', '')}** ({t.get('outcome', '')}, "
                f"last seen {t.get('last_seen', '')})\n"
            )
    else:
        body_parts.append("_(none right now)_\n")
    body_parts.append("\n## Recent learnings\n\n")
    learnings = profile.get("recent_learnings") or []
    if learnings:
        for ln in learnings:
            body_parts.append(f"- {ln}\n")
    else:
        body_parts.append("_(none in the recent window)_\n")
    body_parts.append("\n## Long-running themes\n\n")
    themes = profile.get("long_running_themes") or []
    if themes:
        for th in themes:
            body_parts.append(
                f"- **{th.get('theme', '')}** — "
                f"{th.get('session_count', '?')} sessions since "
                f"{th.get('first_seen', '')}\n"
            )
    else:
        body_parts.append("_(none yet — needs ≥5 digests across ≥30 days)_\n")
    return "".join(body_parts)


# ---------------------------------------------------------------------------
# Build orchestrator
# ---------------------------------------------------------------------------

def build(
    *,
    brain_root: Path,
    provider: LLMProvider | None = None,
    log: Callable[[str], None] = lambda s: None,
    force: bool = False,
) -> dict:
    """Build (or refresh) PROFILE.md. Returns stats.

    `force=True` rebuilds even if the digest-set SHA matches the
    existing PROFILE.md. Useful for testing or after a system-prompt
    change. Default False: idempotent."""
    brain_root = Path(brain_root)
    md_dir = brain_root / "memory" / "semantic" / "digests"
    profile_path = brain_root / "memory" / "semantic" / "PROFILE.md"

    digests = list(_iter_digests(md_dir))
    stats = {
        "digests_read": len(digests),
        "profile_written": False,
        "skipped_idempotent": False,
    }

    if not digests:
        log("no digests yet; skipping profile build")
        return stats

    current_sha = _digest_set_sha(md_dir)
    if not force and _existing_profile_sha(profile_path) == current_sha:
        stats["skipped_idempotent"] = True
        log("profile up to date (digest_set_sha unchanged)")
        return stats

    if provider is None:
        provider = resolve_provider()

    prompt = _build_prompt(digests)
    # 300s timeout: the profile prompt carries content from N sessions
    # so it's naturally larger than a single-session digest. Codex QA
    # caught the prior 180s default timing out on a real backfill
    # corpus.
    result = provider.invoke(
        SYSTEM_PROMPT, prompt,
        json_schema=PROFILE_SCHEMA, timeout_s=300,
    )
    if result.parsed_json is None:
        # Surface LLMError to caller; do NOT overwrite an existing
        # profile with garbage.
        raise LLMError("profile builder: provider returned no parsed JSON")

    md_body = _render_profile_md(result.parsed_json, current_sha)
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(profile_path, md_body)
    stats["profile_written"] = True
    log(f"wrote {profile_path}")
    return stats


__all__ = ["build", "SYSTEM_PROMPT", "PROFILE_SCHEMA"]


# CLI entry point — run from the command line
if __name__ == "__main__":
    import argparse, os
    p = argparse.ArgumentParser(prog="profile_builder")
    p.add_argument("--brain", default=None)
    p.add_argument("--force", action="store_true")
    args = p.parse_args()
    root = Path(args.brain).expanduser() if args.brain \
           else Path(os.environ.get("BRAIN_ROOT",
                                     str(Path.home() / ".agent")))
    stats = build(brain_root=root, force=args.force, log=print)
    print("---")
    for k, v in stats.items():
        print(f"  {k}: {v}")
