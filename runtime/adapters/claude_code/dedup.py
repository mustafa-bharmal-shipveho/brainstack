"""Per-session injected-doc store (S2).

Auto-recall fires on every substantive prompt. Without a memory of what it
already showed, a long session re-injects the same three documents twenty
times: pure context burn, zero new information, and a hit rate that looks
great while telling the user nothing.

`SessionDedupStore` keeps `{doc path -> sha256 of body}` for one session in
one small JSON file under `<log_dir>/injected/`. Keying on the body hash
rather than the path alone is deliberate — a memory the user just edited
must reach the model again.

Failure policy is fail-open everywhere: a corrupt file behaves as empty
(the worst case is one redundant injection, never a lost prompt), and the
write is atomic so a hook killed mid-write cannot leave a half file behind.

Scaffold: `path` and `_safe_id` are pure formulas and are implemented for
real; `load`/`split`/`record`/`prune` are structure-only (signatures +
docstrings) pending the Development phase. See
tests/runtime/test_dedup_store.py.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from runtime.adapters.claude_code.auto_recall import RecallCandidate


def _safe_id(session_id: str) -> str:
    """Sanitize a (possibly attacker-influenced) session id into a safe
    filename stem. Claude Code's payload is untrusted enough that a `../`
    in it must never escape the injected dir."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id)[:128]
    return safe or "unknown"


class SessionDedupStore:
    """Tracks `{doc path -> sha256 of body}` injected this session."""

    def __init__(self, root: Path, session_id: str):
        self.root = Path(root)
        self.session_id = session_id

    @property
    def path(self) -> Path:
        return self.root / f"{_safe_id(self.session_id)}.json"

    def load(self) -> dict[str, str]:
        """Return `{path: sha256}` for this session. `{}` on missing or
        corrupt file — a corrupt store must never block a prompt.

        Scaffold: signature + docstring only.
        """
        raise NotImplementedError("scaffold")

    def split(
        self, candidates: "list[RecallCandidate]"
    ) -> "tuple[list[RecallCandidate], list[RecallCandidate]]":
        """Partition `candidates` into `(fresh, duplicates)`.

        A candidate is a duplicate iff `load().get(path) == content_sha256`
        (changed content re-injects).

        Scaffold: signature + docstring only.
        """
        raise NotImplementedError("scaffold")

    def record(self, injected: "list[RecallCandidate]") -> None:
        """Merge `injected` into the on-disk store and atomically write it
        back (`os.replace` of a sibling tmp file). Each fire records only
        what IT injected; the file accumulates across fires in a session.

        Scaffold: signature + docstring only.
        """
        raise NotImplementedError("scaffold")

    @staticmethod
    def prune(root: Path, *, max_age_days: int = 7, now: "float | None" = None) -> int:
        """Remove session store files whose mtime is older than
        `max_age_days`. Returns the count removed. A no-op (returns 0) when
        `root` does not exist.

        Scaffold: signature + docstring only.
        """
        raise NotImplementedError("scaffold")


__all__ = ["SessionDedupStore", "_safe_id"]
