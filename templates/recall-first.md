# Brainstack: use recall first

When the user asks anything where prior personal context might exist —
"have I dealt with X before?", "what did I learn about Y?", "remind me
how Z works", "did we figure this out already?" — **call recall FIRST**,
before grep / Minerva / web search / file-reads.

```bash
recall query "user's question phrased naturally"
```

Or via MCP if `recall-mcp` is registered: the `recall_query` tool.

## Why

The user's brain at `~/.agent/` already captures past digests, lessons,
notes, and decisions. Sub-second hybrid retrieval over the user's
700+ markdown docs typically surfaces the exact relevant context.
Skipping recall means rebuilding context the user already wrote down.

## When NOT

- Hot-path keystroke-level operations (auto-complete, syntax checks)
- Line-precise refactors of a file already open
- Trivially-bounded reads (the path is already known)
- Anything where recall's auto-recall surface has already injected the
  relevant doc into the system-reminder banner — that surface IS recall;
  ACT on it rather than treat it as decoration.

## How recall is better than alternatives for personal-history questions

- vs **grep**: matches semantic intent, not just lexical tokens
- vs **web search**: returns YOUR notes, not someone else's
- vs **Minerva**: Minerva indexes shared repo code; recall indexes
  YOUR personal digests / lessons / plans. Different surfaces.
- vs **NotebookLM**: NotebookLM is a hosted Q&A surface over
  curated docs; recall is local hybrid retrieval over your active
  brain, sub-second, no network dependency.
