"""Hook worker: daemon-first retrieval with a conditional fallback.

The UserPromptSubmit hook is a fresh Python subprocess on every prompt.
Loading qdrant + an embedder in that subprocess costs ~1.5 s cold, which is
why auto-recall times out on real machines. The fix is a resident daemon
that holds a warm retriever; the hook talks to it over a Unix socket.

That daemon also OWNS the embedded Qdrant store (it is locked per process),
which makes the fallback policy asymmetric — and that asymmetry is the
whole point of this file:

  - `no_socket` / `connection_refused` → the daemon is DOWN. Nothing holds
    the store, so the in-process path can run. Fall back.
  - `import_error` → `recall.daemon_client` is not installed. Same thing.
  - `timeout` / `server_error` / `protocol_error` → the daemon is ALIVE and
    holding the store lock. An in-process fallback would only block on
    fcntl until the hook's own timeout fires, turning a 800 ms degradation
    into a 1500 ms one and still returning nothing. Do NOT fall back;
    report `unavailable` and move on.

Every test here fakes `hooks._daemon_query`, so no socket is ever opened.

Tests cover: daemon hit (path + query_ms provenance), the no-fallback
timeout rule, both fallback reasons, the degraded flag, index staleness
reporting, and the per-session dedup store landing on disk.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from runtime.adapters.claude_code import hooks as hooks_mod
from runtime.adapters.claude_code.config import RuntimeConfig
from runtime.adapters.claude_code.hooks import handle_hook
from runtime.core.events import load_events

_PROMPT = "what is the incident escalation protocol?"


# ---------- fixtures ----------

@pytest.fixture
def tmp_config(tmp_path: Path) -> RuntimeConfig:
    """Auto-recall ON, everything under tmp. Mirrors the fixture in
    test_auto_recall.py so the two files stay comparable."""
    return RuntimeConfig(
        log_dir=tmp_path / "logs",
        enable_auto_recall=True,
        auto_recall_k=5,
        auto_recall_budget_tokens=1500,
        auto_recall_timeout_ms=1500,
        auto_recall_min_chars=8,
        auto_recall_dedup=True,
    )


@pytest.fixture
def stdin_with(monkeypatch):
    def _set(payload: object) -> None:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        monkeypatch.setattr(sys, "stdin", StringIO(text))
    return _set


# ---------- daemon wire helpers ----------

def _wire_result(path: str, *, body: str = "daemon body",
                 score: float = 0.72, rerank_score: float | None = 0.88,
                 source: str = "brain") -> dict[str, Any]:
    """One entry of the daemon's `results` list, exactly as
    `recall.daemon.result_to_wire` pins it."""
    return {
        "path": path,
        "source": source,
        "title": Path(path).stem.replace("-", " ").title(),
        "name": Path(path).stem,
        "type": "lesson",
        "description": "a description",
        "score": score,
        "rerank_score": rerank_score,
        "provenance": "recall-remember",
        "frontmatter": {"source": "recall-remember",
                        "created": "2026-06-01T00:00:00+00:00"},
        "body": body,
        "content_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }


def _wire_response(results: list[dict[str, Any]] | None = None, *,
                   query_ms: int = 73, degraded: bool = False,
                   reranked: bool = True,
                   index_stale: bool | None = False) -> dict[str, Any]:
    resp: dict[str, Any] = {
        "v": 1,
        "ok": True,
        "results": results if results is not None else [
            _wire_result("/brain/memory/semantic/lessons/escalation.md"),
        ],
        "query_ms": query_ms,
        "degraded": degraded,
        "reranked": reranked,
        "model": {"embedder": "BAAI/bge-base-en-v1.5",
                  "reranker": "jinaai/jina-reranker-v1-turbo-en"},
    }
    if index_stale is not None:
        resp["index_stale"] = index_stale
    return resp


@dataclass
class _FakeQueryResult:
    """In-process retriever result (no rerank score — the fallback path
    never loads a cross-encoder)."""
    path: str
    source: str
    name: str
    score: float
    body: str = ""
    rerank_score: float | None = None
    content_sha256: str = ""


class _RecordingLoader:
    """Stands in for `auto_recall._load_retriever`, counting calls so a
    test can assert the in-process path was never even attempted."""

    def __init__(self, results: list[_FakeQueryResult] | None = None):
        self.calls = 0
        self._results = results if results is not None else [
            _FakeQueryResult(path="/brain/memory/semantic/lessons/inproc.md",
                             source="brain", name="inproc", score=0.66,
                             body="in-process body"),
        ]

    def __call__(self):
        self.calls += 1
        outer = self

        class _R:
            def query(self, prompt: str, *, k: int = 5,
                      type_filter: Any = None, source_filter: Any = None):
                return outer._results[:k]

        return _R()


class _DaemonSpy:
    """Recording stand-in for `hooks._daemon_query`. Records every call so
    a test can assert WHAT the hook asked the daemon for, not merely that
    it asked. A silent arg mismatch here would be invisible in production:
    the daemon would happily answer a k=5 query for a config that says 10,
    or connect to the wrong socket and report `no_socket` forever."""

    def __init__(self, ret: tuple[Any, Any]):
        self.ret = ret
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.ret

    @property
    def only_call(self) -> dict[str, Any]:
        """The single call's arguments, normalized to a kwargs dict so the
        assertions do not care whether `prompt` was passed positionally."""
        assert len(self.calls) == 1, f"expected 1 daemon call, got {len(self.calls)}"
        args, kwargs = self.calls[0]
        merged = dict(kwargs)
        if args:
            merged["prompt"] = args[0]
        return merged


def _patch(monkeypatch, *, daemon_return, loader: _RecordingLoader) -> _DaemonSpy:
    """Wire both seams: the daemon client and the in-process retriever.
    Returns the daemon spy for tests that assert on the request."""
    import runtime.adapters.claude_code.auto_recall as ar_mod
    spy = _DaemonSpy(daemon_return)
    monkeypatch.setattr(hooks_mod, "_daemon_query", spy)
    monkeypatch.setattr(ar_mod, "_load_retriever", loader)
    return spy


def _auto_recall_ext(cfg: RuntimeConfig) -> dict[str, Any]:
    ar = [e for e in load_events(cfg.event_log_path) if e.event == "AutoRecall"]
    assert len(ar) == 1, f"expected one AutoRecall event, got {len(ar)}"
    return dict(ar[0].extensions)


# ---------- the daemon path ----------

class TestDaemonRequest:
    """What the hook SENDS. Everything downstream is only as good as the
    request: a wrong k truncates results silently, a wrong socket path
    makes the daemon look permanently down, and a budget larger than the
    hook's own timeout means the worker gets killed mid-recv and every
    fire is logged as `timeout` instead of the honest reason."""

    def test_daemon_query_receives_prompt_k_and_session(
        self, tmp_config: RuntimeConfig, stdin_with, monkeypatch, capsys
    ):
        spy = _patch(monkeypatch, daemon_return=(_wire_response(), None),
                     loader=_RecordingLoader())
        stdin_with({"session_id": "sess-42", "prompt": _PROMPT})
        handle_hook("UserPromptSubmit", config=tmp_config)

        call = spy.only_call
        assert call["prompt"] == _PROMPT
        assert call["k"] == tmp_config.auto_recall_k
        # The RAW session id goes over the wire; sanitization is a
        # filesystem concern that belongs to the dedup store alone.
        assert call["session_id"] == "sess-42"

    @pytest.mark.parametrize("threshold", [None, -1.9547, 0.0, 0.5])
    def test_min_rerank_reaches_the_builder_verbatim(
        self, tmp_config: RuntimeConfig, stdin_with, monkeypatch, capsys,
        threshold,
    ):
        """The hook is a courier for the rerank floor, not a policy layer.

        `None` is the gate's only off switch, and every float — negative
        included — turns it on. Coercing `None` to `0.0` here would
        silently enable the gate at a threshold that rejects every
        negative cross-encoder logit, which is what the daemon actually
        returns, and auto-recall would go permanently silent."""
        import runtime.adapters.claude_code.auto_recall as ar_mod
        _patch(monkeypatch, daemon_return=(_wire_response(), None),
               loader=_RecordingLoader())

        seen: list[Any] = []
        real = ar_mod.build_recall_block

        def spy(*args, **kwargs):
            seen.append(kwargs.get("min_rerank", "NOT PASSED"))
            return real(*args, **kwargs)

        monkeypatch.setattr(ar_mod, "build_recall_block", spy)
        monkeypatch.setattr(tmp_config, "auto_recall_min_rerank", threshold)

        stdin_with({"session_id": "s", "prompt": _PROMPT})
        handle_hook("UserPromptSubmit", config=tmp_config)

        assert seen == [threshold]

    def test_daemon_socket_defaults_to_env_override(
        self, tmp_config: RuntimeConfig, stdin_with, monkeypatch, capsys
    ):
        """With the config left at its `$BRAIN_ROOT` literal default, the
        `RECALL_DAEMON_SOCKET` env var decides. This is the escape hatch
        the test suite itself relies on to never touch a live daemon."""
        spy = _patch(monkeypatch, daemon_return=(_wire_response(), None),
                     loader=_RecordingLoader())
        stdin_with({"session_id": "s", "prompt": _PROMPT})
        handle_hook("UserPromptSubmit", config=tmp_config)

        assert Path(spy.only_call["socket_path"]) == Path(
            os.environ["RECALL_DAEMON_SOCKET"])

    def test_env_socket_beats_explicit_config(
        self, tmp_path: Path, stdin_with, monkeypatch, capsys
    ):
        """`RECALL_DAEMON_SOCKET` is an OVERRIDE, so it outranks even an
        explicitly configured path. The root conftest guard depends on
        exactly this: it sets the env var for every test, and a config
        value that could outrank it would let some future test reach the
        developer's live socket at `~/.agent/runtime/recall.sock`.
        Orchestrator decision, 2026-09-04."""
        configured = tmp_path / "configured-recall.sock"
        cfg = RuntimeConfig(
            log_dir=tmp_path / "logs",
            enable_auto_recall=True,
            auto_recall_min_chars=8,
            auto_recall_daemon_socket=str(configured),
        )
        spy = _patch(monkeypatch, daemon_return=(_wire_response(), None),
                     loader=_RecordingLoader())
        stdin_with({"session_id": "s", "prompt": _PROMPT})
        handle_hook("UserPromptSubmit", config=cfg)

        resolved = Path(spy.only_call["socket_path"])
        assert resolved == Path(os.environ["RECALL_DAEMON_SOCKET"])
        assert resolved != configured

    def test_explicit_config_socket_used_when_env_unset(
        self, tmp_path: Path, stdin_with, monkeypatch, capsys
    ):
        """Without the override, the configured path is what a user who
        moved their socket actually gets."""
        monkeypatch.delenv("RECALL_DAEMON_SOCKET", raising=False)
        configured = tmp_path / "configured-recall.sock"
        cfg = RuntimeConfig(
            log_dir=tmp_path / "logs",
            enable_auto_recall=True,
            auto_recall_min_chars=8,
            auto_recall_daemon_socket=str(configured),
        )
        spy = _patch(monkeypatch, daemon_return=(_wire_response(), None),
                     loader=_RecordingLoader())
        stdin_with({"session_id": "s", "prompt": _PROMPT})
        handle_hook("UserPromptSubmit", config=cfg)

        assert Path(spy.only_call["socket_path"]) == configured

    def test_budget_is_capped_by_the_hook_timeout(
        self, tmp_path: Path, stdin_with, monkeypatch, capsys
    ):
        """The socket budget can never exceed the worker's own deadline.
        A 800 ms budget under a 300 ms timeout would guarantee the thread
        is abandoned while still waiting on recv."""
        cfg = RuntimeConfig(
            log_dir=tmp_path / "logs",
            enable_auto_recall=True,
            auto_recall_min_chars=8,
            auto_recall_timeout_ms=300,
            auto_recall_daemon_budget_ms=800,
        )
        spy = _patch(monkeypatch, daemon_return=(_wire_response(), None),
                     loader=_RecordingLoader())
        stdin_with({"session_id": "s", "prompt": _PROMPT})
        handle_hook("UserPromptSubmit", config=cfg)

        assert spy.only_call["budget_ms"] == 300

    def test_budget_used_verbatim_when_under_the_timeout(
        self, tmp_config: RuntimeConfig, stdin_with, monkeypatch, capsys
    ):
        spy = _patch(monkeypatch, daemon_return=(_wire_response(), None),
                     loader=_RecordingLoader())
        stdin_with({"session_id": "s", "prompt": _PROMPT})
        handle_hook("UserPromptSubmit", config=tmp_config)

        expected = min(tmp_config.auto_recall_daemon_budget_ms,
                       tmp_config.auto_recall_timeout_ms)
        assert spy.only_call["budget_ms"] == expected
        assert expected == tmp_config.auto_recall_daemon_budget_ms


class TestDaemonPath:

    def test_daemon_hit_uses_daemon_path_and_query_ms(
        self, tmp_config: RuntimeConfig, stdin_with, monkeypatch, capsys
    ):
        loader = _RecordingLoader()
        _patch(monkeypatch, daemon_return=(_wire_response(query_ms=73), None),
               loader=loader)
        stdin_with({"session_id": "s", "prompt": _PROMPT})
        handle_hook("UserPromptSubmit", config=tmp_config)

        out = capsys.readouterr().out
        assert "<system-reminder>" in out
        assert "/brain/memory/semantic/lessons/escalation.md" in out
        assert "daemon body" in out

        ext = _auto_recall_ext(tmp_config)
        assert ext["x_outcome"] == "hit"
        assert ext["x_path"] == "daemon"
        # The daemon measured retrieval itself; the hook must report THAT,
        # not its own stopwatch (which would include socket + JSON time).
        assert ext["x_query_ms"] == 73
        assert ext.get("x_daemon_error") in (None, "")
        assert ext["x_latency_ms"] >= ext["x_query_ms"]
        # The warm path was used, so no embedder was ever constructed.
        assert loader.calls == 0

    def test_daemon_timeout_is_unavailable_without_fallback(
        self, tmp_config: RuntimeConfig, stdin_with, monkeypatch, capsys
    ):
        """A timeout means the daemon is alive and holding the Qdrant
        process lock. Falling back in-process would block on fcntl and
        return nothing anyway, at twice the latency."""
        loader = _RecordingLoader()
        _patch(monkeypatch, daemon_return=(None, "timeout"), loader=loader)
        stdin_with({"session_id": "s", "prompt": _PROMPT})
        rc = handle_hook("UserPromptSubmit", config=tmp_config)

        assert rc == 0
        assert "auto-recall:" not in capsys.readouterr().out
        ext = _auto_recall_ext(tmp_config)
        assert ext["x_outcome"] == "unavailable"
        assert ext["x_path"] == "daemon"
        assert str(ext["x_daemon_error"]).startswith("timeout")
        assert "x_latency_ms" in ext
        assert loader.calls == 0, (
            "in-process fallback ran while the daemon held the store lock"
        )

    def test_connection_refused_falls_back_inproc(
        self, tmp_config: RuntimeConfig, stdin_with, monkeypatch, capsys
    ):
        """Socket file exists but nothing is listening — a crashed daemon.
        Nothing holds the store, so the slow path is legitimate."""
        loader = _RecordingLoader()
        _patch(monkeypatch, daemon_return=(None, "connection_refused"),
               loader=loader)
        stdin_with({"session_id": "s", "prompt": _PROMPT})
        handle_hook("UserPromptSubmit", config=tmp_config)

        assert "in-process body" in capsys.readouterr().out
        ext = _auto_recall_ext(tmp_config)
        assert ext["x_outcome"] == "hit"
        assert ext["x_path"] == "inproc"
        assert str(ext["x_daemon_error"]).startswith("connection_refused")
        assert loader.calls == 1

    def test_import_error_falls_back_inproc(
        self, tmp_config: RuntimeConfig, stdin_with, monkeypatch, capsys
    ):
        """`recall.daemon_client` missing (older install, partial upgrade)
        must degrade to today's behaviour, not disable auto-recall."""
        loader = _RecordingLoader()
        _patch(monkeypatch, daemon_return=(None, "import_error"), loader=loader)
        stdin_with({"session_id": "s", "prompt": _PROMPT})
        handle_hook("UserPromptSubmit", config=tmp_config)

        assert "in-process body" in capsys.readouterr().out
        ext = _auto_recall_ext(tmp_config)
        assert ext["x_outcome"] == "hit"
        assert ext["x_path"] == "inproc"
        assert str(ext["x_daemon_error"]).startswith("import_error")
        assert loader.calls == 1

    def test_degraded_flag_from_daemon_response(
        self, tmp_config: RuntimeConfig, stdin_with, monkeypatch, capsys
    ):
        """`degraded` means the daemon answered from the dense leg alone
        (sparse fallback active). Results are still useful but relevance
        is worse, so the flag has to survive into telemetry — otherwise a
        week of bad hit rates has no explanation."""
        loader = _RecordingLoader()
        _patch(monkeypatch, daemon_return=(_wire_response(degraded=True), None),
               loader=loader)
        stdin_with({"session_id": "s", "prompt": _PROMPT})
        handle_hook("UserPromptSubmit", config=tmp_config)

        ext = _auto_recall_ext(tmp_config)
        assert ext["x_outcome"] == "hit"
        assert ext["x_degraded"] is True

    @pytest.mark.parametrize("stale", [True, False])
    def test_x_index_stale_copied_from_daemon_response(
        self, tmp_config: RuntimeConfig, stdin_with, monkeypatch, capsys,
        stale: bool,
    ):
        loader = _RecordingLoader()
        _patch(monkeypatch,
               daemon_return=(_wire_response(index_stale=stale), None),
               loader=loader)
        stdin_with({"session_id": "s", "prompt": _PROMPT})
        handle_hook("UserPromptSubmit", config=tmp_config)

        ext = _auto_recall_ext(tmp_config)
        assert ext["x_index_stale"] is stale

    def test_x_index_stale_absent_on_inproc_path(
        self, tmp_config: RuntimeConfig, stdin_with, monkeypatch, capsys
    ):
        """Only the daemon tracks index freshness. On the in-process path
        staleness is UNKNOWN, and `false` would be a lie that makes a
        stale-index incident invisible. Omit the key instead."""
        loader = _RecordingLoader()
        _patch(monkeypatch, daemon_return=(None, "no_socket"), loader=loader)
        stdin_with({"session_id": "s", "prompt": _PROMPT})
        handle_hook("UserPromptSubmit", config=tmp_config)

        ext = _auto_recall_ext(tmp_config)
        assert ext["x_path"] == "inproc"
        assert "x_index_stale" not in ext


# ---------- per-session dedup store ----------

class TestDedupStoreWiring:

    def test_dedup_store_written_under_log_dir_injected(
        self, tmp_config: RuntimeConfig, stdin_with, monkeypatch, capsys
    ):
        """The store lives beside the event log so `install.sh` and the
        prune pass have exactly one directory to reason about. The session
        id is sanitized before it becomes a filename."""
        body = "escalation runbook body"
        loader = _RecordingLoader()
        _patch(monkeypatch, daemon_return=(
            _wire_response([_wire_result("/brain/memory/escalation.md",
                                         body=body)]), None,
        ), loader=loader)
        stdin_with({"session_id": "sess/1", "prompt": _PROMPT})
        handle_hook("UserPromptSubmit", config=tmp_config)

        injected_dir = tmp_config.log_dir / "injected"
        store_path = injected_dir / "sess_1.json"
        found = (sorted(p.name for p in injected_dir.glob("*"))
                 if injected_dir.exists() else "<dir missing>")
        assert store_path.exists(), f"no dedup store at {store_path}; found {found}"
        data = json.loads(store_path.read_text(encoding="utf-8"))
        assert data["schema"] == 1
        assert data["session_id"] == "sess/1"
        assert data["injected"] == {
            "/brain/memory/escalation.md":
                hashlib.sha256(body.encode("utf-8")).hexdigest(),
        }

    def test_stale_store_files_pruned_during_a_normal_fire(
        self, tmp_config: RuntimeConfig, stdin_with, monkeypatch, capsys
    ):
        """Nothing else ever cleans this directory. Every session Claude
        Code opens leaves a file behind, so without a prune on the hook
        path the injected dir grows forever on a machine that is never
        explicitly maintained. Seven days is well past any live session,
        so a recent file must survive untouched."""
        injected_dir = tmp_config.log_dir / "injected"
        injected_dir.mkdir(parents=True, exist_ok=True)
        stale = injected_dir / "stale-session.json"
        recent = injected_dir / "recent-session.json"
        for f, session in ((stale, "stale-session"), (recent, "recent-session")):
            f.write_text(json.dumps({
                "schema": 1, "session_id": session,
                "updated_ts": 0.0, "injected": {},
            }), encoding="utf-8")

        now = time.time()
        eight_days = now - 8 * 86400
        one_day = now - 86400
        os.utime(stale, (eight_days, eight_days))
        os.utime(recent, (one_day, one_day))

        _patch(monkeypatch, daemon_return=(_wire_response(), None),
               loader=_RecordingLoader())
        stdin_with({"session_id": "current", "prompt": _PROMPT})
        handle_hook("UserPromptSubmit", config=tmp_config)

        assert not stale.exists(), "8-day-old store survived the prune"
        assert recent.exists(), "1-day-old store was pruned; sessions can outlive a day"
        # The fire's own store still landed — pruning must not race the write.
        assert (injected_dir / "current.json").exists()

    def test_dedup_disabled_by_config(
        self, tmp_path: Path, stdin_with, monkeypatch, capsys
    ):
        """`auto_recall_dedup = false` is the kill switch: no store is
        constructed, so nothing is written and every fire re-injects."""
        cfg = RuntimeConfig(
            log_dir=tmp_path / "logs",
            enable_auto_recall=True,
            auto_recall_min_chars=8,
            auto_recall_dedup=False,
        )
        loader = _RecordingLoader()
        _patch(monkeypatch, daemon_return=(_wire_response(), None),
               loader=loader)
        stdin_with({"session_id": "s", "prompt": _PROMPT})
        handle_hook("UserPromptSubmit", config=cfg)

        assert "auto-recall:" in capsys.readouterr().out
        injected_dir = cfg.log_dir / "injected"
        assert not injected_dir.exists() or not list(injected_dir.glob("*.json"))


# ---------- in-process fallback must stay cheap ----------

class TestInprocLoaderStaysCheap:

    def test_inproc_loader_never_refreshes_index(self, monkeypatch):
        """Index freshness is the DAEMON's job. If the fallback loader
        ever called `needs_refresh` / `build_index`, a single stale file
        would trigger a full re-embed inside a 1500 ms hook subprocess —
        guaranteeing a timeout and, worse, doing it on every prompt."""
        import types

        import recall.config as rcfg_mod
        import recall.core as rcore_mod
        import recall.index as rindex_mod
        from runtime.adapters.claude_code import auto_recall as ar_mod

        def _boom(*a, **kw):
            raise AssertionError(
                "the in-process auto-recall loader must never touch the index"
            )

        monkeypatch.setattr(rindex_mod, "needs_refresh", _boom, raising=False)
        monkeypatch.setattr(rindex_mod, "build_index", _boom, raising=False)

        class _StubRetriever:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def query(self, prompt, *, k=5, **kw):
                return []

        monkeypatch.setattr(rcore_mod, "HybridRetriever", _StubRetriever)

        fake_cfg = types.SimpleNamespace(
            sources=[types.SimpleNamespace(name="brain"),
                     types.SimpleNamespace(name="imports")],
            ranking=types.SimpleNamespace(
                mode="hybrid",
                embedder="BAAI/bge-base-en-v1.5",
                sparse_embedder="Qdrant/bm25",
                reranker="cross_encoder",
                reranker_model="jinaai/jina-reranker-v1-turbo-en",
                rerank_n=20,
                needs_review_policy="demote",
                needs_review_penalty=0.5,
            ),
            auto_recall=types.SimpleNamespace(exclude_sources=[]),
        )
        monkeypatch.setattr(rcfg_mod, "load_config", lambda: fake_cfg)

        retriever = ar_mod._load_retriever()
        assert isinstance(retriever, _StubRetriever)
        # Never pay the cross-encoder load in a per-prompt subprocess, even
        # when the user's config asks for one — the daemon does reranking.
        assert retriever.kwargs.get("reranker") == "none"


# ---------- the record-before-print window --------------------------------

class TestDedupRecordedOnlyAfterTheBlockIsPrinted:
    """`dedup_store.record` marks documents "already shown this session".

    It used to run inside the worker thread, at the end of
    `build_recall_block` — i.e. BEFORE the main thread had printed
    anything. If `t.join(timeout)` expired in the window between that
    write and the `out_q.put`, the hook logged `timeout`, printed nothing,
    and the store on disk still claimed those docs had been shown. Every
    later prompt in the session then deduped them away, so the documents
    the user most needed were unreachable for the rest of the session —
    and the telemetry said `timeout`, never `dedup`.

    The record is therefore a MAIN-THREAD step taken only on the `ok`
    path, after `print(block)` has actually run.
    """

    def test_worker_that_times_out_after_building_records_nothing(
        self, tmp_config: RuntimeConfig, stdin_with, monkeypatch, capsys
    ):
        """The exact race: the block is built, then the worker stalls past
        the hook's deadline. Nothing is printed, so nothing may be marked
        as shown."""
        import runtime.adapters.claude_code.auto_recall as ar_mod

        _patch(monkeypatch, daemon_return=(
            _wire_response([_wire_result("/brain/memory/escalation.md")]), None,
        ), loader=_RecordingLoader())
        monkeypatch.setattr(tmp_config, "auto_recall_timeout_ms", 60)

        real = ar_mod.build_recall_block

        def stall_after_building(*args, **kwargs):
            built = real(*args, **kwargs)
            # The main thread's join(0.06s) expires inside this sleep.
            time.sleep(1.0)
            return built

        monkeypatch.setattr(ar_mod, "build_recall_block", stall_after_building)
        stdin_with({"session_id": "sess-timeout", "prompt": _PROMPT})
        handle_hook("UserPromptSubmit", config=tmp_config)

        out = capsys.readouterr().out
        assert "auto-recall:" not in out, "a timed-out fire must print nothing"
        store = tmp_config.log_dir / "injected" / "sess-timeout.json"
        assert not store.exists(), (
            f"{store.name} was written for a fire the user never saw; those "
            f"docs are now deduped away for the rest of the session"
        )
        assert _auto_recall_ext(tmp_config)["x_outcome"] == "timeout"

    def test_record_runs_on_the_main_thread(
        self, tmp_config: RuntimeConfig, stdin_with, monkeypatch, capsys
    ):
        """Pins WHERE the write happens. The worker is a daemon thread the
        hook can abandon at any instant; only the main thread knows whether
        the block reached the user."""
        import threading

        from runtime.adapters.claude_code.dedup import SessionDedupStore

        _patch(monkeypatch, daemon_return=(
            _wire_response([_wire_result("/brain/memory/escalation.md")]), None,
        ), loader=_RecordingLoader())

        callers: list[str] = []
        real_record = SessionDedupStore.record

        def spy(self, injected):
            callers.append(threading.current_thread().name)
            return real_record(self, injected)

        monkeypatch.setattr(SessionDedupStore, "record", spy)
        stdin_with({"session_id": "sess-main", "prompt": _PROMPT})
        handle_hook("UserPromptSubmit", config=tmp_config)

        assert "auto-recall:" in capsys.readouterr().out
        assert callers == ["MainThread"], (
            f"dedup.record ran on {callers!r}; the worker thread cannot know "
            f"whether the block was printed"
        )
        assert (tmp_config.log_dir / "injected" / "sess-main.json").exists()

    def test_builder_returns_the_injected_candidates(self):
        """`build_recall_block` hands the injected candidates back instead
        of recording them itself, so the caller owns the commit point."""
        from runtime.adapters.claude_code.auto_recall import build_recall_block

        results = [_FakeQueryResult(path="/brain/memory/a.md", source="brain",
                                    name="a", score=0.9, body="body a")]
        block, telemetry, injected = build_recall_block(
            "q", _RecordingLoader(results)(), k=5, budget_tokens=1500,
        )
        assert block
        assert telemetry["x_k_returned"] == 1
        assert [c.path for c in injected] == ["/brain/memory/a.md"]

    def test_builder_does_not_write_the_store_itself(self, tmp_path: Path):
        """Even handed a live store, the builder only READS it (`split`)."""
        from runtime.adapters.claude_code.auto_recall import build_recall_block
        from runtime.adapters.claude_code.dedup import SessionDedupStore

        store = SessionDedupStore(tmp_path / "injected", "s")
        results = [_FakeQueryResult(path="/brain/memory/a.md", source="brain",
                                    name="a", score=0.9, body="body a")]
        _, _, injected = build_recall_block(
            "q", _RecordingLoader(results)(), k=5, budget_tokens=1500,
            dedup_store=store,
        )
        assert injected, "the candidate should have survived the gates"
        assert not store.path.exists(), (
            "build_recall_block wrote the dedup store; the commit point "
            "belongs to the caller that prints the block"
        )


# ---------- socket resolution has exactly one implementation --------------

def test_resolve_daemon_socket_delegates_to_recall_config(
    tmp_config: RuntimeConfig, monkeypatch, tmp_path: Path
):
    """`hooks._resolve_daemon_socket` is a thin pass-through to
    `recall.config.daemon_socket_path` (via the config property). It used
    to carry a full inline re-implementation of the same precedence behind
    a `try/except`; two copies of a resolution order is how the hook and
    the daemon end up on different sockets."""
    import recall.config as rcfg

    seen: list[str | None] = []

    def fake(raw=None):
        seen.append(raw)
        return tmp_path / "from-recall-config.sock"

    monkeypatch.setattr(rcfg, "daemon_socket_path", fake)
    monkeypatch.setattr(tmp_config, "auto_recall_daemon_socket", "/cfg/x.sock")

    assert hooks_mod._resolve_daemon_socket(tmp_config) == (
        tmp_path / "from-recall-config.sock"
    )
    assert seen == ["/cfg/x.sock"]
