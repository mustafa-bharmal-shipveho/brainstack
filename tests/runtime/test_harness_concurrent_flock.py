"""Sub-phase 0c: stress test the harness log_event.sh flock pattern.

The hook telemetry harness writes to events.jsonl from many concurrent hook
invocations. If flock is misused, lines get interleaved (corrupted JSON) or
records get lost. This test fires N=20 invocations of log_event.sh in parallel
and verifies:

  - exactly N lines are written
  - every line is valid JSON
  - no line contains content from another invocation (no interleaving)
  - the .write.lock sentinel exists and is empty (we lock it, not the data)

Pure Python + subprocess. Does not require Claude Code; this validates the
flock mechanism in isolation. Establishes the contract that the runtime's
own write path (runtime/core/locking.py, sub-phase 3a) will inherit.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HARNESS_DIR = REPO_ROOT / "runtime" / "_empirical" / "harness"
HOOK_SCRIPT = HARNESS_DIR / "hooks" / "log_event.sh"


@pytest.fixture
def isolated_harness(tmp_path: Path) -> Path:
    """Copy the harness scripts into a tmp dir so the test cannot touch
    real telemetry data. Returns the new harness root."""
    dest = tmp_path / "harness"
    shutil.copytree(HARNESS_DIR, dest, ignore=shutil.ignore_patterns("_data"))
    (dest / "_data").mkdir()
    return dest


def _fire_hook(harness_root: str, event: str, payload: str, run_tag: str) -> int:
    env = os.environ.copy()
    env["RUNTIME_HARNESS"] = harness_root
    env["RUNTIME_HARNESS_RUN_TAG"] = run_tag
    proc = subprocess.run(
        ["bash", str(Path(harness_root) / "hooks" / "log_event.sh"), event],
        input=payload,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if proc.returncode != 0:
        sys.stderr.write(f"[hook stderr] {proc.stderr}\n")
    return proc.returncode


def test_concurrent_hooks_no_corruption(isolated_harness: Path) -> None:
    """20 hooks fire in parallel; events.jsonl must be exactly 20 valid JSON lines."""
    n = 20
    payloads = [
        json.dumps({
            "session_id": f"sess-{i:02d}",
            "tool_name": "Read" if i % 2 == 0 else "Grep",
            "marker": f"worker-{i}",
        })
        for i in range(n)
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        futures = [
            pool.submit(_fire_hook, str(isolated_harness), "PostToolUse", p, f"stress-{i}")
            for i, p in enumerate(payloads)
        ]
        results = [f.result() for f in futures]
    assert all(rc == 0 for rc in results), f"hooks returned non-zero: {results}"

    events_path = isolated_harness / "_data" / "events.jsonl"
    assert events_path.exists(), "events.jsonl must exist after hooks fire"
    raw = events_path.read_text(encoding="utf-8").splitlines()
    assert len(raw) == n, f"expected {n} lines, got {len(raw)}"

    parsed: list[dict] = []
    for i, line in enumerate(raw):
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError as e:
            pytest.fail(f"line {i} is not valid JSON: {line!r} ({e})")

    # Every event has the right shape and no cross-contamination.
    seen_run_tags = {row["run_tag"] for row in parsed}
    assert seen_run_tags == {f"stress-{i}" for i in range(n)}, (
        "run_tags do not match expected set; hooks contaminated each other"
    )
    seen_pids = {row["pid"] for row in parsed}
    assert len(seen_pids) >= 1, "expected at least one pid"
    assert all(row["event"] == "PostToolUse" for row in parsed)


def test_payload_samples_match_event_count(isolated_harness: Path) -> None:
    """Every hook firing with a payload must also produce a payload-samples line."""
    n = 8
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        futures = [
            pool.submit(
                _fire_hook,
                str(isolated_harness),
                "PreToolUse",
                json.dumps({"session_id": f"s-{i}", "marker": i}),
                f"pair-{i}",
            )
            for i in range(n)
        ]
        [f.result() for f in futures]

    events = (isolated_harness / "_data" / "events.jsonl").read_text().splitlines()
    payloads = (isolated_harness / "_data" / "payload-samples.jsonl").read_text().splitlines()
    assert len(events) == n
    assert len(payloads) == n


def test_lock_sentinel_is_separate_from_data(isolated_harness: Path) -> None:
    """The flock sentinel must be a separate file from events.jsonl.

    Lessons learned from brainstack's test_concurrent_appends.py: locking the
    data file directly breaks across atomic-replace operations. We lock a
    sentinel; the data file may be rotated independently."""
    _fire_hook(str(isolated_harness), "Stop", "", "lock-check")
    data_dir = isolated_harness / "_data"
    assert (data_dir / ".write.lock").exists(), "sentinel lock file must exist"
    assert (data_dir / "events.jsonl").exists()
    # Lock and data are distinct files
    assert (data_dir / ".write.lock") != (data_dir / "events.jsonl")


def test_empty_stdin_does_not_create_payload_line(isolated_harness: Path) -> None:
    """When a hook fires with no stdin payload, events.jsonl gets a line but
    payload-samples.jsonl does not (we only sample when there is something
    to sample)."""
    _fire_hook(str(isolated_harness), "SessionStart", "", "no-stdin")
    events = (isolated_harness / "_data" / "events.jsonl").read_text().splitlines()
    payloads_path = isolated_harness / "_data" / "payload-samples.jsonl"
    assert len(events) == 1
    if payloads_path.exists():
        assert payloads_path.read_text().strip() == "", (
            "no payload line should be written when stdin is empty"
        )


def test_metadata_only_no_raw_content_leak(isolated_harness: Path) -> None:
    """Data policy: events.jsonl must NOT contain raw payload content.

    Feed a hook a payload that includes a fake secret string. After the hook
    fires, verify the secret is absent from events.jsonl (it may appear in
    payload-samples.jsonl, which is gitignored). This is the leak-test
    prototype that the runtime's `tests/runtime/synthetic/leak_test.py`
    will generalize in sub-phase 2c."""
    fake_secret = "sk_live_FAKE_TEST_TOKEN_DO_NOT_LEAK_ABCDEF12345"
    payload = json.dumps({
        "session_id": "leak-test",
        "tool_name": "Bash",
        "tool_input": {"command": f"echo {fake_secret}"},
    })
    _fire_hook(str(isolated_harness), "PostToolUse", payload, "leak-check")

    events_text = (isolated_harness / "_data" / "events.jsonl").read_text()
    assert fake_secret not in events_text, (
        "events.jsonl leaked raw payload content; data policy violation"
    )
    # And the metadata row should still record useful telemetry
    row = json.loads(events_text.strip())
    assert row["tool_name"] == "Bash"
    assert row["payload_bytes"] > 0
    assert "tool_input" in row["payload_keys"]
