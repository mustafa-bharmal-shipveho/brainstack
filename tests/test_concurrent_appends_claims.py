"""Stress test: concurrent appenders to the claims log.

Mirrors `tests/test_concurrent_appends.py` for the new claims log layer.
Producers will not write to claims.jsonl directly (the consolidator
does), but multiple consolidations or operator override commands can
contend on the same lock. This pins the lock-correctness contract.
"""
import json
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

fcntl = pytest.importorskip("fcntl")


def _worker(claims_path: str, mem_dir: str, worker_id: int, n_rows: int):
    """Append `n_rows` synthetic `assert` events through the claims module."""
    sys.path.insert(0, mem_dir)
    import claims
    for i in range(n_rows):
        sid = f"src-{worker_id:02d}-{i}"
        cid = claims.compute_claim_id("project:test", "release-date", sid)
        fp = claims.compute_value_fingerprint(
            "project:test", "release-date", f"value-{worker_id}-{i}",
        )
        claims.append_assert(
            claims_path,
            claim_id=cid,
            claim_value_fingerprint=fp,
            topic_key="project:test",
            claim_subject="release-date",
            value_normalized=f"value-{worker_id}-{i}",
            value_raw=f"value-{worker_id}-{i}",
            source_event_id=sid,
            source="research-notes",
            source_ts_epoch=1700000000.0 + worker_id * 10 + i,
        )
        time.sleep(0.001)


@pytest.mark.timeout(30)
def test_concurrent_appenders_lose_no_rows(tmp_path):
    brain = tmp_path / ".agent"
    claims_path = str(brain / "memory" / "semantic" / "claims.jsonl")
    mem_dir = str(REPO_ROOT / "agent" / "memory")

    n_workers = 20
    rows_per_worker = 5
    expected = {
        f"src-{w:02d}-{i}"
        for w in range(n_workers) for i in range(rows_per_worker)
    }

    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=_worker,
                    args=(claims_path, mem_dir, w, rows_per_worker))
        for w in range(n_workers)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=25)

    # Every appended row must be present and parseable.
    survived: set = set()
    if Path(claims_path).exists():
        for raw in Path(claims_path).read_text().splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = obj.get("source_event_id")
            if isinstance(sid, str) and sid.startswith("src-"):
                survived.add(sid)

    lost = expected - survived
    assert not lost, (
        f"data loss: {len(lost)}/{len(expected)} rows missing. "
        f"sample: {sorted(list(lost))[:10]}"
    )


def test_no_torn_lines_under_concurrent_writes(tmp_path):
    """Every line in claims.jsonl must be valid JSON. A torn write
    (interleaved bytes from two appenders) would produce JSON decode
    errors. The sentinel-lock pattern is what prevents this; this
    test pins the contract.
    """
    brain = tmp_path / ".agent"
    claims_path = str(brain / "memory" / "semantic" / "claims.jsonl")
    mem_dir = str(REPO_ROOT / "agent" / "memory")

    n_workers = 12
    rows_per_worker = 8

    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=_worker,
                    args=(claims_path, mem_dir, w, rows_per_worker))
        for w in range(n_workers)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=25)

    bad_lines = []
    for raw in Path(claims_path).read_text().splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            json.loads(line)
        except json.JSONDecodeError as exc:
            bad_lines.append((line[:80], str(exc)))
    assert not bad_lines, f"torn lines detected: {bad_lines[:3]}"
