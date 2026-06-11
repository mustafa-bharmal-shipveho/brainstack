# demo.tape - VHS recording script for the brainstack recall demo.
#
# Regenerate with:  make demo        (requires vhs: brew install vhs)
#
# Everything shown comes from the fully synthetic brain built by
# demo/make_demo_brain.py (placeholders only: Acme, Alice). The hidden
# setup block isolates XDG config/cache and the runtime telemetry log so
# a recording NEVER reads or shows the recording machine's real brain,
# real config, or real usage stats.
#
# Beat 3 is `recall stats --since 7d` instead of a raw UserPromptSubmit
# hook render: the hook needs a RECALL_RUNTIME_CONFIG with auto-recall
# enabled plus a warm retriever inside a 3s timeout, which is too fragile
# to re-record deterministically. `--no-tools` keeps the stats beat from
# scanning the recording machine's real ~/.claude/projects transcripts.

Output demo/demo.gif

Set Shell bash
Set Width 1200
Set Height 640
Set FontSize 18
Set TypingSpeed 60ms
Set Padding 16
Set Theme "Catppuccin Mocha"

# --- hidden setup: build the synthetic brain, isolate env, warm the index ---
# The brain lives under /tmp/brainstack-demo (not $PWD) so the doc paths
# recall prints on camera never contain the recording user's home directory.
Hide
Type `python3 demo/make_demo_brain.py /tmp/brainstack-demo/brain`
Enter
Sleep 3s
Type `export BRAIN_ROOT="/tmp/brainstack-demo/brain" XDG_CONFIG_HOME="/tmp/brainstack-demo/.config" XDG_CACHE_HOME="/tmp/brainstack-demo/.cache"`
Enter
Type `export RECALL_RUNTIME_CONFIG="$BRAIN_ROOT/runtime.toml" FASTEMBED_CACHE_PATH="${FASTEMBED_CACHE_PATH:-$HOME/.cache/fastembed}"`
Enter
Sleep 1s
# Warm the index so the first on-camera query is fast. First-ever run may
# also download embedding models into FASTEMBED_CACHE_PATH (one time).
Type `recall reindex`
Enter
Sleep 45s
Type `clear`
Enter
Sleep 1s
Show

# --- beat 0: point recall at the demo brain ---
Type `export BRAIN_ROOT=/tmp/brainstack-demo/brain`
Enter
Sleep 1.5s

# --- beat 1: semantic retrieval over the synthetic lessons ---
# --no-expand: LLM query expansion is fail-open and machine-dependent
# (up to a 20s provider timeout), which is dead air in a recording.
Type `recall query --no-expand "what did I learn about flaky integration tests"`
Enter
Sleep 4s

# --- beat 2: staged dream candidates awaiting human triage ---
Type `recall pending`
Enter
Sleep 3s

# --- beat 3: auto-recall ROI over the last week ---
Type `recall stats --since 7d --no-tools`
Enter
Sleep 3s

Sleep 2s
