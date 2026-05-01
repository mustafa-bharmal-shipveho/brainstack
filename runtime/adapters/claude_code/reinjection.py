"""Re-injection text composer.

Closes the v0.x inject loop. The runtime, on `UserPromptSubmit`, builds a
short text block of "things the user wants the model to consider" and
emits it to stdout. Claude Code may append hook stdout to the model's
prompt — that's the entire mechanism by which the runtime stops being
purely observational.

Design constraints:
  - **Pure function.** No I/O, no clock reads. Easy to test.
  - **Bounded.** The text block has a hard token budget. Truncate by
    priority before exceeding it.
  - **Wrapped.** A clear delimiter `<!-- runtime-reinject -->` so the
    user can grep their prompts and see exactly what the runtime added.
  - **Honest.** If nothing useful to say, return empty string. Never
    pad to look busy.

Priority order when fitting into the budget:
  1. Pinned + currently-injected items (full content)
  2. User-add items since the last UserPromptSubmit (full content)
  3. User-evicted item ids (just the ids — short)

Pinned wins because the user explicitly marked them. User-adds are next
because they're the most recent active intent. User-evicts are last
because all we need is a short id list.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from runtime.core.events import EventRecord
from runtime.core.manifest import InjectionItemSnapshot, Manifest

REINJECT_OPEN = "<!-- runtime-reinject -->"
REINJECT_CLOSE = "<!-- /runtime-reinject -->"


@dataclass
class ReinjectionContext:
    """Everything build_reinjection_block needs. Kept as a single struct
    so callers can build it once + the function stays pure."""

    manifest: Manifest
    user_added_items: list[InjectionItemSnapshot]
    user_evicted_ids: list[str]
    item_content_by_id: dict[str, str]  # id -> raw content for pinned/added items
    budget_tokens: int = 1500
    chars_per_token: int = 4  # rough conversion for budget enforcement


def build_reinjection_block(ctx: ReinjectionContext) -> str:
    """Compose the re-injection text block. Returns empty string if nothing useful."""
    sections: list[str] = []
    char_budget = ctx.budget_tokens * ctx.chars_per_token

    pinned = [it for it in ctx.manifest.items if it.pinned]
    if pinned:
        block = ["User has marked these as always-relevant:"]
        for it in sorted(pinned, key=lambda x: x.id):
            content = ctx.item_content_by_id.get(it.id, "").strip()
            block.append(f"- {it.source_path} (id={it.id})")
            if content:
                block.append(_indent(content))
        sections.append("\n".join(block))

    if ctx.user_added_items:
        block = ["User just added these for this turn:"]
        for it in ctx.user_added_items:
            content = ctx.item_content_by_id.get(it.id, "").strip()
            block.append(f"- {it.source_path} (id={it.id})")
            if content:
                block.append(_indent(content))
        sections.append("\n".join(block))

    if ctx.user_evicted_ids:
        ids = ", ".join(sorted(set(ctx.user_evicted_ids)))
        sections.append(
            f"User has explicitly removed these item ids: {ids}; "
            f"do not rely on them."
        )

    if not sections:
        return ""

    body = "\n\n".join(sections)
    body = _truncate_to_chars(body, char_budget)
    return f"{REINJECT_OPEN}\n{body}\n{REINJECT_CLOSE}"


def _indent(s: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in s.splitlines())


def _truncate_to_chars(s: str, max_chars: int) -> str:
    """Truncate to a hard char budget. Keeps complete lines where possible."""
    if len(s) <= max_chars:
        return s
    # Keep the first max_chars - margin, append a marker line.
    margin = 80
    keep = max(0, max_chars - margin)
    truncated = s[:keep]
    # Trim back to last full line so the marker isn't mid-word
    last_nl = truncated.rfind("\n")
    if last_nl > 0:
        truncated = truncated[:last_nl]
    truncated += (
        f"\n... [truncated to fit re-injection budget of {max_chars} chars] ..."
    )
    return truncated


def collect_user_intent_events(
    events: list[EventRecord], since_ts_ms: int = 0
) -> tuple[list[InjectionItemSnapshot], list[str]]:
    """Walk the event log and pull out user-add snapshots + user-evict ids
    that happened since `since_ts_ms`.

    Used by the hook to assemble the ReinjectionContext.
    """
    added: list[InjectionItemSnapshot] = []
    evicted: list[str] = []
    for ev in events:
        if ev.ts_ms < since_ts_ms:
            continue
        if ev.intent == "user-add":
            for snap in ev.items_added:
                if isinstance(snap, InjectionItemSnapshot):
                    added.append(snap)
        elif ev.intent == "user-evict":
            evicted.extend(ev.item_ids_evicted)
    return added, evicted


__all__ = [
    "REINJECT_CLOSE",
    "REINJECT_OPEN",
    "ReinjectionContext",
    "build_reinjection_block",
    "collect_user_intent_events",
]
