"""Tests for `agent.tools.sdk_cli`.

Exercises each subcommand against tmp_path brain-root, verifies round-trip
through the underlying `agent.memory.sdk` module, and checks error exit codes.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(args, brain_root: Path, stdin: str | None = None, env_extra: dict | None = None):
    cmd = [sys.executable, "-m", "agent.tools.sdk_cli", *args, "--brain-root", str(brain_root)]
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin"}
    import os
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        input=stdin,
        text=True,
        capture_output=True,
        env=env,
    )


# --- append-episodic ------------------------------------------------

def test_append_episodic_inline_event(tmp_path):
    res = _run(
        ["append-episodic", "--namespace", "inbox", "--event", json.dumps({"hello": "world"})],
        brain_root=tmp_path,
    )
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["hello"] == "world"
    assert out["schema_version"] == 1
    assert "ts" in out

    path = tmp_path / "memory" / "episodic" / "inbox" / "AGENT_LEARNINGS.jsonl"
    assert path.exists()
    line = path.read_text().splitlines()[0]
    parsed = json.loads(line)
    assert parsed["hello"] == "world"


def test_append_episodic_stdin(tmp_path):
    payload = json.dumps({"kind": "surface_event", "x": 42})
    res = _run(
        ["append-episodic", "--namespace", "inbox", "--event-stdin"],
        brain_root=tmp_path,
        stdin=payload,
    )
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["x"] == 42


def test_append_episodic_default_namespace_path(tmp_path):
    res = _run(
        ["append-episodic", "--namespace", "default", "--event", json.dumps({"a": 1})],
        brain_root=tmp_path,
    )
    assert res.returncode == 0
    # default ns => no extra subdir
    p = tmp_path / "memory" / "episodic" / "AGENT_LEARNINGS.jsonl"
    assert p.exists()


def test_append_episodic_invalid_namespace(tmp_path):
    res = _run(
        ["append-episodic", "--namespace", "BadName", "--event", "{}"],
        brain_root=tmp_path,
    )
    assert res.returncode == 3
    err = json.loads(res.stderr.strip().splitlines()[-1])
    assert err["code"] == "invalid_namespace"


def test_append_episodic_bad_json(tmp_path):
    res = _run(
        ["append-episodic", "--namespace", "inbox", "--event", "not-json"],
        brain_root=tmp_path,
    )
    assert res.returncode == 2
    err = json.loads(res.stderr.strip().splitlines()[-1])
    assert err["code"] == "invalid_args"


def test_append_episodic_event_must_be_object(tmp_path):
    res = _run(
        ["append-episodic", "--namespace", "inbox", "--event", "[1,2,3]"],
        brain_root=tmp_path,
    )
    assert res.returncode == 2


# --- query-semantic -------------------------------------------------

def test_query_semantic_empty(tmp_path):
    res = _run(["query-semantic", "--namespace", "inbox"], brain_root=tmp_path)
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == []


def test_query_semantic_returns_lessons(tmp_path):
    sem_dir = tmp_path / "memory" / "semantic" / "inbox"
    sem_dir.mkdir(parents=True)
    rows = [
        {"claim": "alpha", "why": "because A", "how_to_apply": "do A"},
        {"claim": "beta",  "why": "because B", "how_to_apply": "do B"},
        {"claim": "gamma", "why": "because G", "how_to_apply": "do G"},
    ]
    with open(sem_dir / "lessons.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    res = _run(["query-semantic", "--namespace", "inbox", "--k", "2"], brain_root=tmp_path)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert len(out) == 2
    assert out[-1]["claim"] == "gamma"

    res2 = _run(
        ["query-semantic", "--namespace", "inbox", "--query", "beta"],
        brain_root=tmp_path,
    )
    assert res2.returncode == 0
    matches = json.loads(res2.stdout)
    assert len(matches) == 1
    assert matches[0]["claim"] == "beta"


def test_query_semantic_invalid_namespace(tmp_path):
    res = _run(["query-semantic", "--namespace", "BAD"], brain_root=tmp_path)
    assert res.returncode == 3


def test_query_semantic_bad_k(tmp_path):
    res = _run(["query-semantic", "--namespace", "inbox", "--k", "0"], brain_root=tmp_path)
    assert res.returncode == 2


# --- read/write policy ----------------------------------------------

def test_write_then_read_policy_json(tmp_path):
    policy = {"version": 1, "rules": [{"id": "r1", "when": "x", "then": "y", "source": "user"}]}
    res = _run(
        ["write-policy", "--namespace", "inbox", "--policy", json.dumps(policy)],
        brain_root=tmp_path,
    )
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == {"ok": True}

    res2 = _run(["read-policy", "--namespace", "inbox"], brain_root=tmp_path)
    assert res2.returncode == 0, res2.stderr
    assert json.loads(res2.stdout) == policy


def test_write_policy_yaml(tmp_path):
    pytest.importorskip("yaml")
    yaml_text = "version: 2\nrules: []\n"
    res = _run(
        ["write-policy", "--namespace", "inbox", "--policy", yaml_text],
        brain_root=tmp_path,
    )
    assert res.returncode == 0, res.stderr

    res2 = _run(["read-policy", "--namespace", "inbox"], brain_root=tmp_path)
    assert res2.returncode == 0
    assert json.loads(res2.stdout) == {"version": 2, "rules": []}


def test_read_policy_missing_returns_empty(tmp_path):
    res = _run(["read-policy", "--namespace", "inbox"], brain_root=tmp_path)
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == {}


def test_write_policy_invalid_namespace(tmp_path):
    res = _run(
        ["write-policy", "--namespace", "BAD", "--policy", "{}"],
        brain_root=tmp_path,
    )
    assert res.returncode == 3


def test_write_policy_bad_json(tmp_path):
    res = _run(
        ["write-policy", "--namespace", "inbox", "--policy", "{not-json"],
        brain_root=tmp_path,
    )
    assert res.returncode == 2


def test_write_policy_stdin(tmp_path):
    policy = {"version": 9, "rules": []}
    res = _run(
        ["write-policy", "--namespace", "inbox", "--policy-stdin"],
        brain_root=tmp_path,
        stdin=json.dumps(policy),
    )
    assert res.returncode == 0, res.stderr


# --- argparse-level errors ------------------------------------------

def test_unknown_subcommand(tmp_path):
    res = _run(["nope"], brain_root=tmp_path)
    assert res.returncode != 0


def test_missing_namespace(tmp_path):
    res = _run(["append-episodic", "--event", "{}"], brain_root=tmp_path)
    assert res.returncode != 0
