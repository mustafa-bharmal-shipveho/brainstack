"""Dream-cycle clusterer registry tests for v0.2-rc1.

Covers register/unregister/list/get, run_all aggregation, and end-to-end
default-namespace registration that delegates to auto_dream.run.
"""
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "memory"))
sys.path.insert(0, str(REPO_ROOT / "agent" / "dream"))
sys.path.insert(0, str(REPO_ROOT / "agent" / "harness"))
sys.path.insert(0, str(REPO_ROOT / "agent" / "harness" / "hooks"))

import registry  # noqa: E402
import sdk  # noqa: E402


@pytest.fixture
def clean_registry():
    """Each test starts with an empty registry."""
    for ns in registry.registered_namespaces():
        registry.unregister_clusterer(ns)
    yield
    for ns in registry.registered_namespaces():
        registry.unregister_clusterer(ns)


@pytest.fixture
def brain(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path))
    return tmp_path


def test_register_unregister_list(clean_registry):
    assert registry.registered_namespaces() == []

    def fn(brain_root=None, dry_run=False):
        return {"namespace": "x", "candidates_written": 0}

    registry.register_clusterer("inbox", fn)
    assert "inbox" in registry.registered_namespaces()
    assert registry.get_clusterer("inbox") is fn
    assert registry.get_clusterer("nope") is None

    # Idempotent re-register.
    registry.register_clusterer("inbox", fn)
    assert registry.registered_namespaces() == ["inbox"]

    registry.unregister_clusterer("inbox")
    assert "inbox" not in registry.registered_namespaces()
    # Idempotent unregister.
    registry.unregister_clusterer("inbox")


def test_register_clusterer_validates_args(clean_registry):
    with pytest.raises(ValueError):
        registry.register_clusterer("", lambda **kw: {})
    with pytest.raises(ValueError):
        registry.register_clusterer("inbox", "not-callable")


def test_run_all_invokes_each_and_aggregates(clean_registry, brain):
    calls = []

    def make(ns):
        def fn(brain_root=None, dry_run=False):
            calls.append((ns, brain_root, dry_run))
            return {"namespace": ns, "candidates_written": 3}
        return fn

    registry.register_clusterer("inbox", make("inbox"))
    registry.register_clusterer("research", make("research"))

    results = registry.run_all(brain_root=str(brain), dry_run=True)
    assert set(results.keys()) == {"inbox", "research"}
    assert results["inbox"]["candidates_written"] == 3
    assert results["research"]["candidates_written"] == 3
    # Each fn was called once with the right kwargs.
    assert sorted(c[0] for c in calls) == ["inbox", "research"]
    for _ns, br, dr in calls:
        assert br == str(brain)
        assert dr is True


def test_default_clusterer_registers_default_namespace(clean_registry):
    registry.register_default_clusterer()
    assert "default" in registry.registered_namespaces()
    assert registry.get_clusterer("default") is not None


def test_run_all_returns_namespace_in_each_result(clean_registry, brain):
    """A clusterer that omits 'namespace' still gets it filled in by run_all."""
    def fn(brain_root=None, dry_run=False):
        return {"candidates_written": 0}  # missing namespace key
    registry.register_clusterer("inbox", fn)
    out = registry.run_all(brain_root=str(brain))
    assert out["inbox"]["namespace"] == "inbox"


def test_namespaced_clusterer_writes_candidates_under_namespace(
    clean_registry, brain
):
    """A clusterer staging candidates writes to <ns>/candidates/, not top level."""
    cands = brain / "memory" / "candidates" / "inbox"
    cands.mkdir(parents=True)

    def stage_one(brain_root=None, dry_run=False):
        # Mimic what a real clusterer would do.
        target = Path(brain_root) / "memory" / "candidates" / "inbox" / "x.json"
        target.write_text(json.dumps({
            "id": "x", "claim": "test claim long enough to pass validate",
            "status": "staged",
        }))
        return {"namespace": "inbox", "candidates_written": 1}

    registry.register_clusterer("inbox", stage_one)
    results = registry.run_all(brain_root=str(brain))
    assert results["inbox"]["candidates_written"] == 1
    assert (cands / "x.json").exists()
    # Default-ns candidates dir untouched.
    default_cands = brain / "memory" / "candidates"
    json_in_default = [p for p in default_cands.iterdir()
                       if p.is_file() and p.suffix == ".json"]
    assert json_in_default == []


def test_default_clusterer_runs_auto_dream_for_default_namespace(
    clean_registry, brain
):
    """register_default_clusterer + run_all must execute the v0.1 logic."""
    # Seed minimal episodic file so auto_dream.run finds it.
    ep_dir = brain / "memory" / "episodic"
    ep_dir.mkdir(parents=True)
    (ep_dir / "AGENT_LEARNINGS.jsonl").write_text("")

    registry.register_default_clusterer()
    results = registry.run_all(brain_root=str(brain))
    assert "default" in results
    assert results["default"]["namespace"] == "default"
    assert "candidates_written" in results["default"]
