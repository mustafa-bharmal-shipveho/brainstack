#!/usr/bin/env bash
# log_event.sh <event_name>
#
# Generic event logger for the runtime/_empirical hook telemetry harness.
# Writes ONE JSON line per invocation to:
#   $RUNTIME_HARNESS/_data/events.jsonl       (safe-to-share metadata)
#   $RUNTIME_HARNESS/_data/payload-samples.jsonl  (full stdin payload, gitignored)
#
# Data policy:
#   events.jsonl              : event name + timestamp + session id + pid + payload KEY LIST
#   payload-samples.jsonl     : full stdin payload (for sub-phase 0b schema analysis)
# payload-samples.jsonl is gitignored. Never commit it without redaction.
#
# Atomicity: each line is written with `>>` after being assembled in a buffer,
# preceded by `flock` on a sentinel file. Concurrent hooks do not corrupt JSONL.

set -euo pipefail

EVENT="${1:-unknown}"
HARNESS="${RUNTIME_HARNESS:-}"
if [ -z "$HARNESS" ]; then
  # Fallback: derive from script location
  HARNESS="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

DATA_DIR="$HARNESS/_data"
mkdir -p "$DATA_DIR"
LOCK="$DATA_DIR/.write.lock"

# Capture stdin (Claude Code hook payload). May be empty.
STDIN_BUF=""
if ! [ -t 0 ]; then
  STDIN_BUF="$(cat || true)"
fi

# Timestamp in ms since epoch
TS_MS=$(python3 -c 'import time; print(int(time.time()*1000))' 2>/dev/null || echo "0")

# Try to extract session_id from stdin JSON (if present)
SESSION_ID=""
if [ -n "$STDIN_BUF" ]; then
  SESSION_ID=$(python3 -c '
import json, sys
try:
    d = json.loads(sys.argv[1])
    print(d.get("session_id") or d.get("sessionId") or "")
except Exception:
    print("")
' "$STDIN_BUF" 2>/dev/null || echo "")
fi

# Tool name (PostToolUse-only convenience)
TOOL_NAME=$(python3 -c '
import json, sys
try:
    d = json.loads(sys.argv[1])
    print(d.get("tool_name") or d.get("toolName") or "")
except Exception:
    print("")
' "$STDIN_BUF" 2>/dev/null || echo "")

# Top-level keys in payload (no values, safe metadata)
PAYLOAD_KEYS=$(python3 -c '
import json, sys
try:
    d = json.loads(sys.argv[1])
    print(",".join(sorted(d.keys())))
except Exception:
    print("")
' "$STDIN_BUF" 2>/dev/null || echo "")

# Approximate payload size in bytes
PAYLOAD_BYTES=${#STDIN_BUF}

# Marker passed in via env to correlate this run with a synthetic session
RUN_TAG="${RUNTIME_HARNESS_RUN_TAG:-}"

# Assemble the metadata-only event line
EVENT_LINE=$(python3 -c '
import json, sys
print(json.dumps({
    "ts_ms": int(sys.argv[1]),
    "event": sys.argv[2],
    "session_id": sys.argv[3],
    "pid": int(sys.argv[4]),
    "tool_name": sys.argv[5],
    "payload_keys": sys.argv[6],
    "payload_bytes": int(sys.argv[7]),
    "run_tag": sys.argv[8],
    "cwd": sys.argv[9],
}, sort_keys=True))
' "$TS_MS" "$EVENT" "$SESSION_ID" "$$" "$TOOL_NAME" "$PAYLOAD_KEYS" "$PAYLOAD_BYTES" "$RUN_TAG" "$PWD")

# flock-guarded append
{
  flock -x 9
  echo "$EVENT_LINE" >> "$DATA_DIR/events.jsonl"
  if [ -n "$STDIN_BUF" ]; then
    # Wrap full payload with the same metadata header for offline analysis
    PAYLOAD_LINE=$(python3 -c '
import json, sys
try:
    raw = json.loads(sys.argv[2])
except Exception:
    raw = {"_parse_error": True, "raw": sys.argv[2][:500]}
print(json.dumps({
    "ts_ms": int(sys.argv[1]),
    "event": sys.argv[3],
    "session_id": sys.argv[4],
    "pid": int(sys.argv[5]),
    "run_tag": sys.argv[6],
    "payload": raw,
}, sort_keys=True, default=str))
' "$TS_MS" "$STDIN_BUF" "$EVENT" "$SESSION_ID" "$$" "$RUN_TAG")
    echo "$PAYLOAD_LINE" >> "$DATA_DIR/payload-samples.jsonl"
  fi
} 9>"$LOCK"

# Hooks must always exit 0 unless explicitly trying to block; we never block.
exit 0
