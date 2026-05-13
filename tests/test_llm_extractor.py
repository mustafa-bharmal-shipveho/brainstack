"""Tests for the LLM-based claim extractor.

Coverage:
  - Implements the TopicKeyExtractor Protocol (drop-in compat).
  - Reads the predicate library from ExtractorConfig (framework-shaped
    — adding a predicate is a config change).
  - Uses resolve_provider() — the user's existing LLM CLI is honored
    via env var / config / auto-detect.
  - Producer-agnostic: prompt NEVER contains event['source'].
  - Per-event cache produces idempotent re-runs (no double-spend).
  - Cache-invalidation on schema_version bump.
  - JSON-schema-enforced output. Invalid responses produce zero claims.
  - At most one claim per (topic, subject) slot — matches storage
    invariant.
  - No producer name branching in the module (string + AST scan).
  - End-to-end with a synthetic non-agentry producer.
"""
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "memory"))

import llm_extractor  # noqa: E402
import topic_keys  # noqa: E402


# --- Fakes ------------------------------------------------------------

class _FakeResult:
    def __init__(self, parsed_json, provider="fake", model="fake-1"):
        self.text = json.dumps(parsed_json)
        self.parsed_json = parsed_json
        self.tokens_in = 100
        self.tokens_out = 50
        self.provider = provider
        self.model = model
        self.cost_usd = None


class _FakeProvider:
    """Stand-in for llm_providers.resolve_provider() result.

    Records every invoke call so tests can assert on system+prompt and
    confirm the prompt is built from config (not hardcoded)."""
    def __init__(self, scripted_response):
        self.scripted_response = scripted_response
        self.calls = []  # list of (system, prompt, model, schema)

    def invoke(self, system, prompt, *, model=None, json_schema=None,
               max_budget_usd=0.02, timeout_s=30):
        self.calls.append({
            "system": system, "prompt": prompt,
            "model": model, "json_schema": json_schema,
        })
        resp = self.scripted_response
        if callable(resp):
            resp = resp(system, prompt)
        return _FakeResult(resp)


def _stub_extractor(tmp_path, scripted_response,
                    predicates=None) -> llm_extractor.LLMExtractor:
    """Build an LLMExtractor with a fake provider pre-injected."""
    brain = str(tmp_path / ".agent")
    cfg = topic_keys.ExtractorConfig(
        predicates=predicates or topic_keys.DEFAULT_PREDICATES,
    )
    ex = llm_extractor.LLMExtractor(brain_root=brain, namespace="default",
                                    config=cfg)
    # Pre-resolve the provider via object.__setattr__ so we bypass the
    # llm_providers.resolve_provider() call.
    fp = _FakeProvider(scripted_response)
    object.__setattr__(ex, "_provider", fp)
    object.__setattr__(
        ex, "_system_prompt",
        llm_extractor._build_system_prompt(cfg),
    )
    return ex, fp


# --- Prompt building from config -------------------------------------

def test_prompt_includes_every_configured_predicate():
    """Adding a predicate to ExtractorConfig MUST surface in the prompt.
    This is the framework rule — the LLM extractor learns about new
    predicates from config, not from hardcoded code."""
    custom_predicate = {
        "match": [],  # not used by LLM; it's a guidance prompt
        "normalizer": "freeform-2k",
    }
    cfg = topic_keys.ExtractorConfig(predicates={
        **topic_keys.DEFAULT_PREDICATES,
        "custom-fact": custom_predicate,
    })
    prompt = llm_extractor._build_system_prompt(cfg)
    assert '"release-date"' in prompt
    assert '"status"' in prompt
    assert '"custom-fact"' in prompt


def test_prompt_lists_enum_values_inline():
    """The `status` enum_values must appear in the prompt so the LLM
    knows the canonical set without us hardcoding it in code."""
    cfg = topic_keys.ExtractorConfig(predicates={
        "status": {
            "match": [], "normalizer": "enum",
            "enum_values": ["blocked", "in-progress", "done"],
        }
    })
    prompt = llm_extractor._build_system_prompt(cfg)
    assert "blocked" in prompt
    assert "in-progress" in prompt
    assert "done" in prompt


def test_prompt_does_not_leak_producer_names():
    """Framework rule: prompt is producer-agnostic. The LLM must NOT
    learn about Slack, Gmail, agentry, etc. from the prompt."""
    cfg = topic_keys.ExtractorConfig()
    prompt = llm_extractor._build_system_prompt(cfg)
    for name in ("slack", "gmail", "agentry", "discord", "calendar"):
        # Allow incidental occurrences in comments? No — the prompt is
        # what the LLM sees. Reject all of them.
        assert name not in prompt.lower(), (
            f"prompt leaks producer name {name!r}; the framework rule "
            f"says extraction must be producer-agnostic"
        )


def test_user_prompt_does_not_include_source_field():
    """The per-event prompt body must NOT include event['source']."""
    event = {
        "body_redacted": "PS2 launches on 2026-05-20",
        "source": "slack",     # MUST NOT appear in prompt
        "event_id": "e:1",
        "channel_id": "C0X",
        "channel_type": "channel",
        "counterparty": "U123",
    }
    prompt = llm_extractor._build_user_prompt(event)
    assert "slack" not in prompt.lower()
    assert "source" not in prompt.lower() or "source_event" not in prompt.lower()
    # But the documented optional fields are fair game
    assert "C0X" in prompt
    assert "U123" in prompt


# --- Extract end-to-end with a fake provider ------------------------

def test_extract_returns_parsed_claims(tmp_path):
    response = {
        "claims": [
            {"topic_key": "project:ps2", "claim_subject": "release-date",
             "value_normalized": "2026-05-20",
             "value_raw": "PS2 launches on 2026-05-20"}
        ]
    }
    ex, fp = _stub_extractor(tmp_path, response)
    out = ex.extract({
        "body_redacted": "PS2 launches on 2026-05-20",
        "source": "research-notes",
        "event_id": "rn:1",
        "source_ts": "1700000000.0",
    })
    assert len(out) == 1
    assert out[0].topic_key == "project:ps2"
    assert out[0].value_normalized == "2026-05-20"
    assert len(fp.calls) == 1  # one LLM call


def test_extract_empty_body_returns_empty_without_calling_llm(tmp_path):
    ex, fp = _stub_extractor(tmp_path, {"claims": []})
    out = ex.extract({"body_redacted": "", "event_id": "e:1"})
    assert out == []
    assert fp.calls == []  # never called


def test_extract_missing_event_id_returns_empty_without_calling_llm(tmp_path):
    ex, fp = _stub_extractor(tmp_path, {"claims": []})
    out = ex.extract({"body_redacted": "PS2 launches on 2026-05-20"})
    assert out == []
    assert fp.calls == []


def test_extract_caches_per_event_id_so_rerun_does_not_call_llm(tmp_path):
    """Idempotency: second call for the same event_id MUST hit the
    on-disk cache and produce zero new LLM calls. Critical for AC-7."""
    response = {
        "claims": [
            {"topic_key": "project:ps2", "claim_subject": "release-date",
             "value_normalized": "2026-05-20",
             "value_raw": "PS2 launches on 2026-05-20"}
        ]
    }
    ex, fp = _stub_extractor(tmp_path, response)
    ev = {"body_redacted": "PS2 launches on 2026-05-20",
          "event_id": "rn:1",
          "source_ts": "1700000000.0",
          "source": "research-notes"}
    out1 = ex.extract(ev)
    out2 = ex.extract(ev)
    assert out1 == out2
    assert len(fp.calls) == 1  # second call hit the cache


def test_extract_cache_invalidates_on_schema_version_bump(tmp_path):
    """A schema_version bump (manual: edit the constant) forces a
    re-extraction so corrupt or outdated prompts get fixed."""
    response = {
        "claims": [
            {"topic_key": "project:ps2", "claim_subject": "release-date",
             "value_normalized": "2026-05-20",
             "value_raw": "PS2"}
        ]
    }
    ex, fp = _stub_extractor(tmp_path, response)
    ev = {"body_redacted": "PS2 launches on 2026-05-20",
          "event_id": "rn:1", "source_ts": "1700000000.0"}
    ex.extract(ev)
    assert len(fp.calls) == 1
    # Manually corrupt the cache to simulate a stale schema_version.
    import os
    p = llm_extractor._cache_path(ex.brain_root, ex.namespace, "rn:1")
    with open(p) as f:
        cached = json.load(f)
    cached["schema_version"] = "99-old"
    with open(p, "w") as f:
        json.dump(cached, f)
    ex.extract(ev)
    assert len(fp.calls) == 2  # re-extracted


def test_extract_skips_when_provider_returns_invalid_json(tmp_path):
    """A response that doesn't validate against the schema produces
    zero claims, doesn't crash, and is NOT cached."""
    # parsed_json=None simulates schema-validation failure (the provider
    # would've raised LLMError; we model the silent path via parsed_json=None).
    class _BadResult:
        text = "garbage"; parsed_json = None
        tokens_in = tokens_out = 0
        provider = "fake"; model = "fake-1"; cost_usd = None
    class _BadProvider:
        calls = 0
        def invoke(self, system, prompt, **kw):
            self.calls += 1
            return _BadResult()
    brain = str(tmp_path / ".agent")
    ex = llm_extractor.LLMExtractor(brain_root=brain, namespace="default")
    bp = _BadProvider()
    object.__setattr__(ex, "_provider", bp)
    object.__setattr__(ex, "_system_prompt", "system")
    out = ex.extract({
        "body_redacted": "PS2 launches on 2026-05-20",
        "event_id": "rn:1", "source_ts": "1700000000.0",
    })
    assert out == []
    # Cache must NOT contain a stale empty entry — re-run should
    # try the LLM again.
    out2 = ex.extract({
        "body_redacted": "PS2 launches on 2026-05-20",
        "event_id": "rn:1", "source_ts": "1700000000.0",
    })
    assert bp.calls == 2


def test_extract_dedupes_same_slot_claims(tmp_path):
    """Storage invariant: at most one claim per (topic, subject) per
    event. If the LLM returns two release-date claims for project:ps2,
    only the first survives."""
    response = {
        "claims": [
            {"topic_key": "project:ps2", "claim_subject": "release-date",
             "value_normalized": "2026-05-18", "value_raw": ""},
            {"topic_key": "project:ps2", "claim_subject": "release-date",
             "value_normalized": "2026-05-20", "value_raw": ""},
        ]
    }
    ex, fp = _stub_extractor(tmp_path, response)
    out = ex.extract({
        "body_redacted": "PS2 launches on 2026-05-18 or 2026-05-20",
        "event_id": "rn:1", "source_ts": "1700000000.0",
    })
    assert len(out) == 1
    assert out[0].value_normalized == "2026-05-18"


def test_extract_with_synthetic_unknown_producer(tmp_path):
    """Framework rule end-to-end: a producer the framework has never
    seen MUST produce identical claims to a known producer for the
    same body."""
    response = {
        "claims": [
            {"topic_key": "project:ps2", "claim_subject": "release-date",
             "value_normalized": "2026-05-20", "value_raw": ""}
        ]
    }
    ex, _ = _stub_extractor(tmp_path, response)
    out_known = ex.extract({
        "body_redacted": "PS2 launches on 2026-05-20",
        "event_id": "k:1", "source": "research-notes",
        "source_ts": "1700000000.0",
    })
    out_unknown = ex.extract({
        "body_redacted": "PS2 launches on 2026-05-20",
        "event_id": "u:1", "source": "fictitious-future-producer-9000",
        "source_ts": "1700000000.0",
    })
    assert [c.value_normalized for c in out_known] == \
           [c.value_normalized for c in out_unknown]


# --- Framework shape (AC-6) ------------------------------------------

def test_module_has_no_producer_name_branching():
    src = (REPO_ROOT / "agent" / "memory" / "llm_extractor.py").read_text()
    for name in ("slack", "gmail", "agentry", "discord", "calendar",
                 "teams", "research-notes", "nbeditor"):
        for bad in (f'"{name}" ==', f'== "{name}"',
                    f"'{name}' ==", f"== '{name}'",
                    f'in ["{name}"', f'in ("{name}"',
                    f"in ['{name}'", f"in ('{name}'"):
            assert bad not in src, (
                f"llm_extractor.py contains producer-name branch: {bad!r}"
            )


def test_module_does_not_import_producer_sdks():
    src = (REPO_ROOT / "agent" / "memory" / "llm_extractor.py").read_text()
    for bad in ("import slack_sdk", "from slack_sdk",
                "import agentry", "from agentry",
                "import googleapiclient", "from googleapiclient"):
        assert bad not in src


# --- Protocol conformance --------------------------------------------

def test_implements_topic_key_extractor_protocol():
    """Drop-in compatibility: LLMExtractor must be usable wherever
    HeuristicExtractor is."""
    ex = llm_extractor.LLMExtractor(brain_root="/tmp/test-brain")
    assert hasattr(ex, "extract")
    assert callable(ex.extract)


# --- Provider resolution honors the user's setup ---------------------

# --- HybridExtractor coordinator -------------------------------------

class _ScriptedExtractor:
    """Minimal TopicKeyExtractor stand-in. Returns whatever Claim list
    was scripted in the constructor; records how many times it was
    called."""
    def __init__(self, claims):
        self.scripted = claims
        self.calls = 0
    def extract(self, event):
        self.calls += 1
        return list(self.scripted)


def test_hybrid_runs_primary_only_when_primary_emits_claims():
    """Documented intent: heuristic precedence. When the primary
    extractor emits ≥1 claim, the fallback (LLM) is NOT consulted —
    saves a token spend per event."""
    primary_claim = topic_keys.Claim(
        topic_key="project:ps2", claim_subject="release-date",
        value_normalized="2026-05-20", value_raw="",
    )
    fallback_claim = topic_keys.Claim(
        topic_key="project:ps2", claim_subject="release-date",
        value_normalized="2026-05-19", value_raw="",
    )
    primary = _ScriptedExtractor([primary_claim])
    fallback = _ScriptedExtractor([fallback_claim])
    hybrid = topic_keys.HybridExtractor(primary=primary, fallback=fallback)
    out = hybrid.extract({"body_redacted": "PS2 launches on 2026-05-20",
                          "event_id": "e:1"})
    assert len(out) == 1
    assert out[0].value_normalized == "2026-05-20"  # primary wins
    assert primary.calls == 1
    assert fallback.calls == 0  # fallback skipped


def test_hybrid_falls_back_when_primary_emits_nothing():
    """When the heuristic returns no claims, the LLM fills the gap."""
    fallback_claim = topic_keys.Claim(
        topic_key="project:ps2", claim_subject="status",
        value_normalized="blocked", value_raw="",
    )
    primary = _ScriptedExtractor([])
    fallback = _ScriptedExtractor([fallback_claim])
    hybrid = topic_keys.HybridExtractor(primary=primary, fallback=fallback)
    out = hybrid.extract({"body_redacted": "havent figured this out yet",
                          "event_id": "e:1"})
    assert len(out) == 1
    assert out[0].value_normalized == "blocked"
    assert primary.calls == 1
    assert fallback.calls == 1


def test_hybrid_returns_empty_when_both_emit_nothing():
    primary = _ScriptedExtractor([])
    fallback = _ScriptedExtractor([])
    hybrid = topic_keys.HybridExtractor(primary=primary, fallback=fallback)
    out = hybrid.extract({"body_redacted": "thanks", "event_id": "e:1"})
    assert out == []
    assert primary.calls == 1
    assert fallback.calls == 1


def test_default_extractors_hybrid_mode_returns_single_wrapped_extractor(tmp_path):
    """Verify the registry returns a HybridExtractor (NOT a list of two
    raw extractors that would both run + clobber each other)."""
    cfg_dir = tmp_path / "brainstack"
    cfg_dir.mkdir()
    (cfg_dir / "extractors.toml").write_text(
        '[extractor]\nmode = "hybrid"\n'
    )
    ex = topic_keys.default_extractors(
        config_dir=str(cfg_dir),
        brain_root=str(tmp_path / ".agent"),
        namespace="default",
    )
    assert len(ex) == 1
    assert isinstance(ex[0], topic_keys.HybridExtractor)


def test_provider_resolution_respects_brain_llm_provider_env(monkeypatch, tmp_path):
    """Setting `BRAIN_LLM_PROVIDER=codex` makes the extractor use that
    provider — the same env var the digest pipeline already uses, so
    one knob configures the whole brain's LLM choice."""
    monkeypatch.setenv("BRAIN_LLM_PROVIDER", "claude-code")
    # We don't actually run extraction — we just verify the resolver
    # reads the env. Import the resolver and confirm it returns the
    # named provider.
    sys.path.insert(0, str(REPO_ROOT / "agent" / "tools"))
    from llm_providers import resolve_provider, PROVIDERS
    p = resolve_provider()
    assert p is PROVIDERS["claude-code"]
