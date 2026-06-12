# demo.tape - VHS recording script for the brainstack demo.
#
# Regenerate with:  make demo        (requires vhs: brew install vhs)
#
# Four beats, one arc, against the README's own claims:
#   1. Proactive injection: the UserPromptSubmit hook enriches a prompt
#      with a past lesson, before you ask ("it already knew").
#   2. Audit: `recall trace` walks that same lesson back to its provenance
#      and originating session digest (the headline: memory you can audit).
#   3. Trust gate: a piped (non-TTY) `recall remember --reviewed` is
#      REFUSED; the default write lands staged for human review.
#   4. ROI: `recall stats` over the synthetic week of telemetry.
#
# Everything shown comes from the fully synthetic brain built by
# demo/make_demo_brain.py (placeholders only: Acme, Alice). The hidden
# setup block isolates XDG config/cache and the runtime telemetry log so
# a recording NEVER reads or shows the recording machine's real brain,
# real config, or real usage stats.
#
# Beat 1 invokes the hook exactly the way Claude Code does (hooks.py with
# the event name + JSON on stdin). The demo runtime.toml raises
# auto_recall_timeout_ms to 20s because a fresh hook process loads the
# dense model from disk; the production default stays 3000ms. Recorded
# via the repo venv python so runtime deps (qdrant) resolve.
#
# Beat 3's refusal works inside a pty recording because the pipe (`echo |`)
# makes the hook-relevant fd, stdin, a non-TTY for that command, which is
# exactly how an agent or injected prompt would drive it.

Output demo/demo.gif

Set Shell bash
Set Width 1200
Set Height 700
Set FontSize 18
Set TypingSpeed 55ms
Set Padding 16
Set Theme "Catppuccin Mocha"

# --- hidden setup: build the synthetic brain, isolate env, warm the index ---
# The brain lives under /tmp/brainstack-demo (not $PWD) so the doc paths
# recall prints on camera never contain the recording user's real home
# directory. The rm makes re-recording idempotent: beat 3 writes a staged
# lesson, which would otherwise collide ("lesson already exists") on the
# next take.
Hide
Type `rm -rf /tmp/brainstack-demo`
Enter
Type `python3 demo/make_demo_brain.py /tmp/brainstack-demo/brain`
Enter
Sleep 3s
Type `export BRAIN_ROOT="/tmp/brainstack-demo/brain" XDG_CONFIG_HOME="/tmp/brainstack-demo/.config" XDG_CACHE_HOME="/tmp/brainstack-demo/.cache"`
Enter
Type `export RECALL_RUNTIME_CONFIG="$BRAIN_ROOT/runtime.toml" FASTEMBED_CACHE_PATH="${FASTEMBED_CACHE_PATH:-$HOME/.cache/fastembed}" PYTHONPATH="$PWD"`
Enter
Sleep 1s
# Warm the index so the on-camera hook call spends its budget on model
# load, not index build. First-ever run may also download embedding
# models into FASTEMBED_CACHE_PATH (one time).
Type `recall reindex`
Enter
Sleep 45s
Type `clear`
Enter
Sleep 1s
Show

# --- beat 1: proactive injection (the hook fires before you ask) ---
Type `# every prompt you type runs brainstack's hook. watch it inject a past lesson:`
Enter
Sleep 1.5s
Type `echo '{"prompt": "my queue workers keep claiming the same job twice"}' | .venv/bin/python3 runtime/adapters/claude_code/hooks.py UserPromptSubmit`
Enter
Sleep 9s

# --- beat 2: audit the memory it just injected ---
Type `# where did that lesson come from? trace it back to its source:`
Enter
Sleep 1.5s
Type `recall trace skip-locked`
Enter
Sleep 5s

# --- beat 3: durable memory is human-gated ---
Type `# agents cannot silently write durable memory. piped = not a human:`
Enter
Sleep 1.5s
Type `echo | recall remember "Acme analytics must use the read replica" --reviewed`
Enter
Sleep 4s
Type `recall remember "Acme analytics must use the read replica"`
Enter
Sleep 4s

# --- beat 4: auto-recall ROI over the last week ---
Type `recall stats --since 7d --no-tools`
Enter
Sleep 4s

Sleep 2.5s
