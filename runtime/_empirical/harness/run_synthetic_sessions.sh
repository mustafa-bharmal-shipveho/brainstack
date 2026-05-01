#!/usr/bin/env bash
# run_synthetic_sessions.sh
#
# Fires N short non-interactive `claude -p` sessions with the harness settings
# overlay, to gather hook telemetry. Each session uses a distinct RUN_TAG so
# the aggregator can compute per-session deliverability.
#
# Usage:
#   bash runtime/_empirical/harness/run_synthetic_sessions.sh [N] [PROFILE]
#
# Defaults: N=10 sessions, PROFILE=mixed (varies prompts to exercise tools).
#
# Output: writes events to runtime/_empirical/harness/_data/events.jsonl
#         writes per-run expected counts to .../_data/expected_runs.json

set -euo pipefail

HARNESS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
N="${1:-10}"
PROFILE="${2:-mixed}"

DATA_DIR="$HARNESS/_data"
mkdir -p "$DATA_DIR"

export RUNTIME_HARNESS="$HARNESS"

# Reset the data files so each harness run is a clean read
: > "$DATA_DIR/events.jsonl"
: > "$DATA_DIR/payload-samples.jsonl"
: > "$DATA_DIR/expected_runs.json.tmp"

# Mixed prompts that exercise different tools
prompts_mixed=(
  "What is 2 + 2? Answer in one number."
  "Read the file ${HARNESS}/README.md and summarize it in one sentence."
  "Run 'ls /tmp' and tell me how many entries the listing has."
  "Search this directory for the word 'event' using grep and tell me the file with the most matches."
)
# Compaction-touching prompts (long, many tool calls)
prompts_long=(
  "Read every .md file under ${HARNESS} and produce a one-line summary for each."
)
# Quiet prompts (no tool use expected)
prompts_quiet=(
  "Reply with exactly the word 'ok' and nothing else."
)

case "$PROFILE" in
  mixed) PROMPTS=("${prompts_mixed[@]}") ;;
  long)  PROMPTS=("${prompts_long[@]}") ;;
  quiet) PROMPTS=("${prompts_quiet[@]}") ;;
  *)     echo "Unknown profile: $PROFILE" >&2; exit 2 ;;
esac

echo "[harness] firing $N sessions with profile '$PROFILE'"
i=0
while [ "$i" -lt "$N" ]; do
  TAG="run-$(date +%Y%m%dT%H%M%S)-$i"
  PROMPT="${PROMPTS[$((i % ${#PROMPTS[@]}))]}"
  export RUNTIME_HARNESS_RUN_TAG="$TAG"

  # Expected events: every session SHOULD produce SessionStart + UserPromptSubmit + Stop.
  # PreToolUse / PostToolUse counts depend on the prompt; we record only the
  # invariants here. The aggregator uses these for deliverability % on invariants.
  EXPECTED='{"SessionStart":1,"UserPromptSubmit":1,"Stop":1}'
  echo "[harness] tag=$TAG prompt=\"${PROMPT:0:60}...\""

  # Fire the session. --no-session-persistence keeps things hermetic.
  # --settings overlays our hooks alongside the user's existing user/project
  # hooks. We deliberately do NOT pass --bare because that disables ALL hooks,
  # including the harness ones we are trying to measure. The user's existing
  # hooks will fire as well, but the aggregator filters by RUNTIME_HARNESS_RUN_TAG
  # so foreign events are not counted.
  if ! printf '%s' "$PROMPT" | claude --print \
       --settings "$HARNESS/settings.json" \
       --no-session-persistence \
       --output-format text \
       --model haiku \
       --permission-mode auto \
       --allowedTools Read Grep Glob \
       >/dev/null 2>"$DATA_DIR/.last-stderr"; then
    echo "[harness] WARN session $TAG returned non-zero; stderr: $(tail -c 400 "$DATA_DIR/.last-stderr")" >&2
  fi

  # Append this run's expected to the manifest
  python3 -c "
import json, os, sys
path = sys.argv[1]
tag = sys.argv[2]
expected = json.loads(sys.argv[3])
manifest = {'runs': {}}
if os.path.exists(path) and os.path.getsize(path) > 0:
    try:
        manifest = json.loads(open(path).read())
    except Exception:
        pass
manifest['runs'][tag] = expected
open(path, 'w').write(json.dumps(manifest, indent=2, sort_keys=True))
" "$DATA_DIR/expected_runs.json" "$TAG" "$EXPECTED"

  i=$((i+1))
done

echo "[harness] done. ran $N sessions."
echo "[harness] events: $(wc -l < "$DATA_DIR/events.jsonl") lines"
echo "[harness] payloads: $(wc -l < "$DATA_DIR/payload-samples.jsonl") lines"
echo "[harness] aggregate with: python3 $HARNESS/aggregator.py --expected expected_runs.json"
