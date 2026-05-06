#!/usr/bin/env python3
"""Eval harness for auto-recall: does the injected context actually
improve answers, or is it noise?

The 4/10 confidence score on end-user value comes from this question
being unanswered. We measure what gets retrieved, not what helps. This
harness produces side-by-side prompt pairs you can run through Claude
manually (or pipe into an automated grader) and compare.

How it works:

  1. Read a list of prompts from a file (one per line) — or pull the N
     most recent real user prompts from ~/.claude/projects.
  2. For each prompt:
       a. Run `should_skip()` — record skip-vs-fire.
       b. If fire: build the recall block via `build_recall_block()`.
       c. Capture: prompt, would-skip?, telemetry, full injected block.
  3. Write a markdown grading sheet:

         ## Prompt 1: <text>
         **would_skip**: false  · **k_returned**: 5
         **sources**: brain=3, imports=2  · **top_scores**: 0.78/0.71/0.66

         <details><summary>injected context block (click to expand)</summary>

         ```
         <auto-recall block>
         ```

         </details>

         **Run A** (no auto-recall):  paste Claude.app answer here →
         **Run B** (with auto-recall): paste Claude.app answer here →
         **Verdict**: A / B / tie / both-bad        **Notes**:

To grade:

  - Open the sheet.
  - For each prompt, run it twice in fresh Claude.app sessions:
      A: just paste the prompt
      B: paste the recall block as a system reminder + the prompt
  - Fill in both runs and the verdict.
  - Run `eval/auto_recall_harness.py --score <sheet>` to compute
    the win/lose/tie distribution.

Without an evaluation pass like this, the auto-recall feature's value is
asserted, not measured.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from runtime.adapters.claude_code.auto_recall import (  # noqa: E402
    build_recall_block,
    should_skip,
)


def _load_retriever_or_none():
    """Build the production retriever, or return None on import error.

    NOTE on side effects: ``_load_retriever()`` calls ``recall.config.load_config()``
    which will create ``~/.config/recall/config.json`` on first run, and
    ``HybridRetriever`` will create ``~/.cache/recall/qdrant/`` and ensure
    empty collections if missing. Run this script only after ``recall doctor``
    is clean — running on a fresh install will silently bootstrap recall.

    The harness still produces useful output without the retriever (just
    the would-skip column for each prompt) — useful for sanity-checking
    skip behavior without touching qdrant.
    """
    try:
        from runtime.adapters.claude_code.auto_recall import _load_retriever
        return _load_retriever()
    except Exception as exc:
        print(f"warning: retriever unavailable ({exc}); skipping recall blocks",
              file=sys.stderr)
        return None


def _load_production_defaults() -> dict:
    """Load runtime config so harness defaults track production values."""
    try:
        from runtime.adapters.claude_code.config import RuntimeConfig
        cfg = RuntimeConfig.load()
        return {
            "k": cfg.auto_recall_k,
            "budget": cfg.auto_recall_budget_tokens,
            "min_chars": cfg.auto_recall_min_chars,
            "min_score": cfg.auto_recall_min_score,
        }
    except Exception:
        return {"k": 5, "budget": 1500, "min_chars": 8, "min_score": 0.0}


def _read_prompts(source: str | None, n: int) -> list[str]:
    """Read prompts from a file (one per line) or pull recent from transcripts."""
    if source and source != "-":
        path = Path(source).expanduser()
        text = path.read_text(encoding="utf-8")
        return [ln.strip() for ln in text.splitlines() if ln.strip()]

    transcripts_dir = Path.home() / ".claude" / "projects"
    if not transcripts_dir.is_dir():
        print(f"transcripts dir missing: {transcripts_dir}", file=sys.stderr)
        return []

    rows: list[tuple[int, str]] = []
    for jsonl in transcripts_dir.rglob("*.jsonl"):
        try:
            with jsonl.open("r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(rec, dict):
                        continue
                    if rec.get("type") != "user":
                        continue
                    msg = rec.get("message") or {}
                    if msg.get("role") != "user":
                        continue
                    content = msg.get("content")
                    text: str | None = None
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text = block.get("text", "")
                                break
                    if not text or not isinstance(text, str):
                        continue
                    text = text.strip()
                    if len(text) < 30:
                        continue
                    if text.startswith("<") and text.endswith(">"):
                        continue
                    if text.startswith("[Request interrupted"):
                        continue
                    if text.startswith("This session is being continued"):
                        continue
                    ts = rec.get("timestamp", "")
                    if isinstance(ts, str):
                        try:
                            from datetime import datetime
                            ts_ms = int(datetime.fromisoformat(
                                ts.replace("Z", "+00:00")
                            ).timestamp() * 1000)
                        except Exception:
                            ts_ms = 0
                    else:
                        ts_ms = int(ts) if isinstance(ts, (int, float)) else 0
                    rows.append((ts_ms, text))
        except OSError:
            continue
    rows.sort(key=lambda r: r[0], reverse=True)
    seen: set[str] = set()
    deduped: list[str] = []
    for _, text in rows:
        key = text[:200]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(text)
        if len(deduped) >= n:
            break
    return deduped


def _produce_sheet(prompts: list[str], out_path: Path,
                   *, k: int, budget_tokens: int, min_chars: int,
                   min_score: float) -> None:
    retriever = _load_retriever_or_none()

    lines = [
        "# Auto-recall eval sheet",
        "",
        f"Prompts: {len(prompts)}  ·  k={k}  ·  budget={budget_tokens}  ·  "
        f"min_chars={min_chars}  ·  min_score={min_score}",
        "",
        "## How to grade",
        "",
        "For each prompt below:",
        "",
        "1. Run prompt in a fresh Claude session **without** the recall block. Paste answer under **Run A**.",
        "2. Start another fresh session, paste the recall block (the system-reminder block) **then** the prompt. Paste answer under **Run B**.",
        "3. Decide which answer was better and fill in **Verdict**: `A`, `B`, `tie`, or `both-bad`.",
        "",
        "Then: `eval/auto_recall_harness.py --score <this-file>`",
        "",
        "## Prompts",
        "",
    ]

    summary: Counter[str] = Counter()

    for i, prompt in enumerate(prompts, start=1):
        skip, reason = should_skip(prompt, min_chars=min_chars)
        if skip:
            summary[f"skip:{reason}"] += 1
        block_text = ""
        telemetry: dict = {}
        if not skip and retriever is not None:
            try:
                block_text, telemetry = build_recall_block(
                    prompt, retriever, k=k, budget_tokens=budget_tokens,
                    min_score=min_score,
                )
                summary["fired"] += 1
            except Exception as exc:
                summary[f"error:{type(exc).__name__}"] += 1
                telemetry = {"x_outcome": f"error:{exc}"}

        snippet_one_line = prompt.replace("\n", " ")
        if len(snippet_one_line) > 100:
            snippet_one_line = snippet_one_line[:97] + "..."

        lines.append(f"### Prompt {i}: {snippet_one_line}")
        lines.append("")
        lines.append("**Full prompt:**")
        lines.append("")
        lines.append("```")
        lines.append(prompt)
        lines.append("```")
        lines.append("")
        if skip:
            lines.append(f"**would_skip:** true  ·  **reason:** `{reason}`")
            lines.append("")
            lines.append("(auto-recall would not fire on this prompt — "
                         "no A/B grading needed, mark verdict `n/a`)")
        else:
            outcome = telemetry.get("x_outcome", "?")
            k_ret = telemetry.get("x_k_returned", 0)
            sources = telemetry.get("x_sources", {})
            scores = telemetry.get("x_top_scores", [])
            latency = telemetry.get("x_latency_ms", 0)
            sources_str = ", ".join(f"{s}={n}" for s, n in sources.items())
            scores_str = "/".join(f"{s:.2f}" for s in scores) if scores else "n/a"
            lines.append(
                f"**outcome:** {outcome}  ·  **k_returned:** {k_ret}  ·  "
                f"**latency:** {latency}ms"
            )
            lines.append(
                f"**sources:** {sources_str}  ·  **top_scores:** {scores_str}"
            )
            lines.append("")
            lines.append("<details><summary>injected recall block (click to expand)</summary>")
            lines.append("")
            lines.append("```")
            lines.append(block_text or "(empty — no surfaces above min_score)")
            lines.append("```")
            lines.append("")
            lines.append("</details>")
        lines.append("")
        lines.append("**Run A** (no auto-recall):")
        lines.append("")
        lines.append("```")
        lines.append("(paste fresh-session answer here)")
        lines.append("```")
        lines.append("")
        lines.append("**Run B** (with auto-recall):")
        lines.append("")
        lines.append("```")
        lines.append("(paste fresh-session answer with recall block prepended here)")
        lines.append("```")
        lines.append("")
        lines.append("**Verdict:** _____ (A | B | tie | both-bad | n/a)  ·  **Notes:**")
        lines.append("")
        lines.append("---")
        lines.append("")

    lines.extend([
        "## Summary",
        "",
        "Pre-grading:",
        "",
    ])
    for k_, v in summary.most_common():
        lines.append(f"- {k_}: {v}")
    lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {len(prompts)} prompts to {out_path}")
    print("pre-grading summary:", dict(summary))


_VERDICT_RE = re.compile(r"\*\*Verdict:\*\*\s*([A-Za-z\-/]+)", re.IGNORECASE)


def _score_sheet(sheet_path: Path) -> int:
    text = sheet_path.read_text(encoding="utf-8")
    verdicts: Counter[str] = Counter()
    blocks = text.split("\n---\n")
    graded = 0
    for block in blocks:
        m = _VERDICT_RE.search(block)
        if not m:
            continue
        v = m.group(1).strip().lower()
        if v in ("_____", "n", "na", ""):
            continue
        if v not in {"a", "b", "tie", "both-bad", "both", "n/a"}:
            print(f"  unknown verdict: {v!r}", file=sys.stderr)
            continue
        verdicts[v] += 1
        graded += 1

    if graded == 0:
        print("no graded verdicts found; fill in **Verdict** lines first",
              file=sys.stderr)
        return 1

    print(f"\nGraded prompts: {graded}")
    for v, n in verdicts.most_common():
        print(f"  {v:10s}  {n:3d}  ({n/graded:.0%})")

    a = verdicts["a"]
    b = verdicts["b"]
    if a + b > 0:
        win_rate_b = b / (a + b)
        print(f"\nB-vs-A (excluding ties / n/a): B wins {win_rate_b:.0%} of head-to-head")
        if win_rate_b > 0.55:
            print("  → auto-recall is meaningfully helping (>55% wins)")
        elif win_rate_b > 0.45:
            print("  → auto-recall is roughly neutral (45-55%)")
        else:
            print("  → auto-recall is hurting (<45% wins) — investigate scores or budget")

    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--prompts", default=None,
                   help="path to prompts file (one per line); '-' or omitted = "
                        "auto-pull recent from ~/.claude/projects")
    defaults = _load_production_defaults()
    p.add_argument("--n", type=int, default=20,
                   help="number of recent prompts to pull when no --prompts (default: 20)")
    p.add_argument("--k", type=int, default=defaults["k"],
                   help=f"top-k for retrieval (production default: {defaults['k']})")
    p.add_argument("--budget", type=int, default=defaults["budget"],
                   help=f"injected-context token budget (production default: {defaults['budget']})")
    p.add_argument("--min-chars", type=int, default=defaults["min_chars"],
                   help=f"should_skip min-char threshold (production default: {defaults['min_chars']})")
    p.add_argument("--min-score", type=float, default=defaults["min_score"],
                   help=f"min similarity score floor (production default: {defaults['min_score']})")
    p.add_argument("--out", default="/tmp/auto-recall-eval.md",
                   help="output sheet path (default: /tmp/auto-recall-eval.md)")
    p.add_argument("--score", metavar="SHEET",
                   help="score a previously-graded sheet")
    args = p.parse_args(argv)

    if args.score:
        return _score_sheet(Path(args.score))

    prompts = _read_prompts(args.prompts, args.n)
    if not prompts:
        print("no prompts found", file=sys.stderr)
        return 2
    out_path = Path(args.out).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _produce_sheet(
        prompts, out_path,
        k=args.k, budget_tokens=args.budget, min_chars=args.min_chars,
        min_score=args.min_score,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
