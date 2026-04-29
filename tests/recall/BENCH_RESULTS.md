# Retrieval benchmark — recall vs no-recall

Reproduce: `python tests/recall/bench_e2e.py --report` (from brainstack repo root, with `.venv` active).

The harness builds a deterministic synthetic brain (80 lessons across 8 conceptual buckets, fixed
seed) at `$TMPDIR/brainstack-bench-*/memory/semantic/lessons/` and runs 20 eval queries (10 lexical,
10 paraphrase) through four retrieval strategies. Latency is per-query wall clock, warm-cache for
recall (mirrors what `recall query` does in production after one `recall reindex`).

## Slug-exact recall@5

The query was tagged to a specific lesson; the strategy must surface that exact lesson in top-5.

| Strategy | Overall | Lexical | Paraphrase | p50 ms | p95 ms |
|---|---|---|---|---|---|
| Without recall (index-only) | 50% | 100% | 0% | 0.2 | 0.3 |
| Without recall (index + reads) | 50% | 100% | 0% | 1.5 | 3.9 |
| With recall (BM25-only, warm) | 60% | 100% | 20% | 0.1 | 0.1 |
| With recall (hybrid, warm) | 70% | 100% | 40% | 7.6 | 70.2 |

## Bucket-recall@5

The query is satisfied if any lesson from the right conceptual bucket is in top-5. Closer to real
use: when the user asks "production is on fire at 2am" they want *any* incident-response lesson,
not necessarily one specific one.

| Strategy | Overall | Lexical | Paraphrase | p50 ms | p95 ms |
|---|---|---|---|---|---|
| Without recall (index-only) | 65% | 100% | 30% | 0.2 | 0.3 |
| Without recall (index + reads) | 70% | 100% | 40% | 1.5 | 3.9 |
| With recall (BM25-only, warm) | 95% | 100% | 90% | 0.1 | 0.1 |
| With recall (hybrid, warm) | **100%** | 100% | **100%** | 7.6 | 70.2 |

## Reading the table

- **Lexical recall is 100% everywhere** — even substring matching catches queries that share words
  with the description. The choice of strategy doesn't matter for queries you wrote with the
  memory-file's exact vocabulary in mind.
- **Paraphrase recall is the differentiator.** The user almost never types the exact wording the
  lesson author used. Without recall, paraphrase coverage is 30-40% (slug-exact: 0%). With hybrid
  recall, it's 100% (slug-exact: 40%).
- **Latency for the warm path is dominated by the embedding forward pass** (7.6 ms p50). BM25-only
  is sub-millisecond. Both are negligible compared to a single LLM round-trip.
- **`index + reads` is what a careful agent does today** — read 10 candidate files and rerank by
  body overlap. It costs an order of magnitude more wall-clock than BM25-only via recall, and
  delivers materially worse quality on paraphrases. For agent contexts this also means 10× the
  context bloat from those file reads.

## Methodology / honesty notes

- 80 lessons is small. At 800 lessons the gap between BM25-only and hybrid would likely widen
  (BM25 hit rate falls off as the corpus grows and lexical matches scatter); at 5000+ a vector
  DB starts paying off, but recall stays in the same shape.
- The 20-query eval set is not large enough for tight confidence intervals. Treat the numbers as
  directional, not exact. The harness is seeded so the *same* numbers come out each run on the
  same machine — useful for spotting regressions in the retriever code.
- The "without recall" baselines simulate what an agent does when it only has `MEMORY.md` auto-
  loaded at session start. They're optimistic — a real agent without retrieval also has to *find*
  MEMORY.md and decide whether to read it; the bench gives free access. So the actual gap in
  practice is a bit wider than these numbers show.
- Hybrid p95 latency (70 ms) is dominated by the first MiniLM call after the model is loaded but
  before warm-up. Subsequent calls are closer to p50.

## Cost vs. value

| | Cost | Value |
|---|---|---|
| Add `recall query` to the agent's path | 0.1 ms (BM25) — 7.6 ms (hybrid) per call | +60-70 percentage points on paraphrase recall |
| Skip retrieval, rely on MEMORY.md auto-load | 0 ms | 30-40% paraphrase recall; agent re-explains "things you already taught it" |

For an agent doing a single LLM round-trip per turn (~1 s), 7.6 ms is rounding error. The
quality improvement on paraphrased queries is what closes the loop on "the agent should remember
what I told it last week, even if I phrase it differently this week."
