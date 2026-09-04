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

See tests/runtime/test_dedup_store.py for the pinned contract.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from runtime.adapters.claude_code.auto_recall import RecallCandidate

# Bumped only if the on-disk shape changes. A file carrying an unknown
# schema is treated like a corrupt one: ignored, then overwritten.
STORE_SCHEMA = 1


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
        corrupt file — a corrupt store must never block a prompt."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return {}
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        if not isinstance(data, dict):
            return {}
        injected = data.get("injected")
        if not isinstance(injected, dict):
            return {}
        # Drop anything that isn't a str->str pair rather than trusting a
        # hand-edited file: a non-string sha would compare unequal forever
        # and silently disable dedup for that path.
        return {
            str(k): v for k, v in injected.items() if isinstance(v, str)
        }

    def split(
        self, candidates: "list[RecallCandidate]"
    ) -> "tuple[list[RecallCandidate], list[RecallCandidate]]":
        """Partition `candidates` into `(fresh, duplicates)`.

        A candidate is a duplicate iff `load().get(path) == content_sha256`
        (changed content re-injects)."""
        seen = self.load()
        fresh: "list[RecallCandidate]" = []
        duplicates: "list[RecallCandidate]" = []
        for c in candidates:
            if seen.get(c.path) == c.content_sha256 and c.content_sha256:
                duplicates.append(c)
            else:
                fresh.append(c)
        return fresh, duplicates

    def record(self, injected: "list[RecallCandidate]") -> None:
        """Merge `injected` into the on-disk store and atomically write it
        back (`os.replace` of a sibling tmp file). Each fire records only
        what IT injected; the file accumulates across fires in a session.

        Fail-open: a store we cannot write is a dedup we do not get, not a
        prompt the user does not get."""
        if not injected:
            return
        merged = self.load()
        merged.update({c.path: c.content_sha256 for c in injected})
        payload = json.dumps(
            {
                "schema": STORE_SCHEMA,
                "session_id": self.session_id,
                "updated_ts": time.time(),
                "injected": merged,
            },
            sort_keys=True,
        )
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            # Sibling temp so `os.replace` stays on one filesystem, and a
            # crash mid-write leaves the PREVIOUS store intact rather than
            # a truncated one that `load` would then discard wholesale.
            fd, tmp = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=self.root,
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            return

    @staticmethod
    def prune(root: Path, *, max_age_days: int = 7, now: "float | None" = None) -> int:
        """Remove session store files whose mtime is older than
        `max_age_days`. Returns the count removed. A no-op (returns 0) when
        `root` does not exist.

        Nothing else ever cleans this directory, so the hook calls this
        once per fire. Seven days is far past any live session, so a store
        for a session that could still be running is never touched."""
        root = Path(root)
        cutoff = (time.time() if now is None else now) - max_age_days * 86400
        removed = 0
        try:
            entries = list(root.glob("*.json"))
        except OSError:
            return 0
        for f in entries:
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                # Raced with another hook process, or unreadable. Skip it:
                # a file we failed to prune costs bytes, not correctness.
                continue
        return removed


__all__ = ["SessionDedupStore", "_safe_id"]
