#!/usr/bin/env bash
# log_event.sh <event_name>
#
# Generic event logger for the runtime/_empirical hook telemetry harness.
# Writes ONE JSON line per invocation to:
#   $RUNTIME_HARNESS/_data/events.jsonl       (safe-to-share metadata)
#   $RUNTIME_HARNESS/_data/payload-samples.jsonl  (full stdin payload, gitignored)
#
# Locking is handled by hooks/_atomic_append.py (Python fcntl). macOS bash
# does not ship `flock`, so we route the actual append through Python.
#
# Hooks must always exit 0; this script never blocks the host (Claude Code)
# even if telemetry fails internally.

set -euo pipefail

EVENT="${1:-unknown}"
HARNESS="${RUNTIME_HARNESS:-}"
if [ -z "$HARNESS" ]; then
  HARNESS="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

DATA_DIR="$HARNESS/_data"
mkdir -p "$DATA_DIR"

STDIN_BUF=""
if ! [ -t 0 ]; then
  STDIN_BUF="$(cat || true)"
fi

# Build the events row + (optional) payload row in a single python3 invocation,
# then pipe both (NUL-separated) into _atomic_append.py for locked write.
python3 - "$EVENT" "$STDIN_BUF" "$$" "${RUNTIME_HARNESS_RUN_TAG:-}" "$PWD" \
        "$DATA_DIR/events.jsonl" "$DATA_DIR/payload-samples.jsonl" <<'PY' || true
import json, sys, time, os

event       = sys.argv[1]
stdin_buf   = sys.argv[2]
pid         = int(sys.argv[3])
run_tag     = sys.argv[4]
cwd         = sys.argv[5]
events_path = sys.argv[6]
payload_path = sys.argv[7]

ts_ms = int(time.time() * 1000)

session_id = ""
tool_name = ""
payload_keys = ""
parsed_payload = None
if stdin_buf:
    try:
        parsed_payload = json.loads(stdin_buf)
        if isinstance(parsed_payload, dict):
            session_id = parsed_payload.get("session_id") or parsed_payload.get("sessionId") or ""
            tool_name = parsed_payload.get("tool_name") or parsed_payload.get("toolName") or ""
            payload_keys = ",".join(sorted(parsed_payload.keys()))
    except Exception:
        pass

events_row = json.dumps({
    "ts_ms":         ts_ms,
    "event":         event,
    "session_id":    session_id,
    "pid":           pid,
    "tool_name":     tool_name,
    "payload_keys":  payload_keys,
    "payload_bytes": len(stdin_buf),
    "run_tag":       run_tag,
    "cwd":           cwd,
}, sort_keys=True)

payload_row = ""
if stdin_buf:
    payload_row = json.dumps({
        "ts_ms":     ts_ms,
        "event":     event,
        "session_id": session_id,
        "pid":       pid,
        "run_tag":   run_tag,
        "payload":   parsed_payload if parsed_payload is not None else {"_parse_error": True, "raw": stdin_buf[:500]},
    }, sort_keys=True, default=str)

# Pipe into the atomic appender via stdin (NUL-separated).
combined = events_row
if payload_row:
    combined += "\x00" + payload_row

import subprocess
helper = os.path.join(os.path.dirname(os.path.realpath(__file__)) if False else "", "")
helper_path = os.path.join(os.path.dirname(os.path.abspath(__file__)) if False else os.path.dirname(os.path.realpath(events_path)), "..")
# Compute helper path off RUNTIME_HARNESS to avoid __file__ ambiguity.
import os as _os
harness_root = _os.environ.get("RUNTIME_HARNESS") or _os.path.dirname(_os.path.dirname(events_path))
helper_path = _os.path.join(harness_root, "hooks", "_atomic_append.py")

subprocess.run(
    ["python3", helper_path, events_path, payload_path],
    input=combined,
    text=True,
    check=False,
)
PY

exit 0
