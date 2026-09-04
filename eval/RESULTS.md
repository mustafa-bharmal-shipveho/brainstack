# Benchmark results

## Auto-recall A/B (retrieval-grounded)

Harness: [`bench_recall_ab.py`](bench_recall_ab.py). Dataset:
[`bench_dataset.json`](bench_dataset.json), a synthetic labeled set of 16
documents and 21 questions. Distractor documents (overlapping vocabulary) and
indirectly-worded questions are included on purpose, so the score reflects
real confusability rather than a trivial lookup.

| Condition | recall@1 | recall@3 | recall@5 | MRR | answer-coverage@5 |
|---|---|---|---|---|---|
| **B: with recall** | 0.905 | 0.952 | 1.000 | 0.940 | 1.000 |
| A: no memory | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 |

Read: with recall on, the supporting note is the single top hit for 90.5% of
questions and lands in the top 5 every time; the answer text is present in the
injected top-5 context for every question (the "re-explanation avoided"
proxy). With an empty brain the agent has none of this.

Reproduce: `make bench` (a few seconds once the embedding model is cached).

### What this does and does not show

- It **does** show that hybrid retrieval surfaces the right memory for clean
  and indirectly-worded questions, including against near-neighbor distractors.
  That is the precondition for recall being useful.
- It does **not** by itself prove an agent's final answer is better, and it is
  a small synthetic set, not a public benchmark. The credible next step is
  **LongMemEval** (multi-session, large, with distractors); the harness already
  ingests its JSON format via `--dataset`, and running the full public set is
  on the roadmap. Numbers will be published whatever they say.

<!-- rerank-gate:start -->
## Rerank relevance gate (S4)

Model: `jinaai/jina-reranker-v1-turbo-en` · pairs: 76 (`graded16` 46, `util24` 18, `util24-multi` 12) · date: 2026-09-04

**Chosen threshold: `auto_recall_min_rerank = -1.9547`** — 42 pairs injected, precision (rel>=1) 0.619, strict precision (rel==2) 0.238, relevance-2 recall 0.769, harmful (rel==0) injected 16.

**P4 acceptance (precision >= 0.65 on the injected set, harmful == 0): DOES NOT HOLD** (precision 0.619 < 0.65; 16 harmful (rel==0) pairs injected).

Observed score range: -4.3238 – 0.8694 (raw cross-encoder outputs, not calibrated probabilities — the threshold does not transfer to another model).

| threshold | injected | precision (rel>=1) | precision (rel==2) | recall (rel==2) | harmful | |
|---:|---:|---:|---:|---:|---:|:--|
| 0.8694 | 1 | 1.000 | 1.000 | 0.077 | 0 |  |
| 0.4030 | 2 | 0.500 | 0.500 | 0.077 | 1 |  |
| 0.2721 | 3 | 0.667 | 0.667 | 0.154 | 1 |  |
| 0.1395 | 4 | 0.750 | 0.750 | 0.231 | 1 |  |
| 0.0074 | 5 | 0.800 | 0.600 | 0.231 | 1 |  |
| -0.1241 | 6 | 0.833 | 0.500 | 0.231 | 1 |  |
| -0.3161 | 7 | 0.714 | 0.429 | 0.231 | 2 |  |
| -0.3508 | 8 | 0.750 | 0.375 | 0.231 | 2 |  |
| -0.4691 | 9 | 0.667 | 0.333 | 0.231 | 3 |  |
| -0.4798 | 10 | 0.700 | 0.300 | 0.231 | 3 |  |
| -0.4951 | 11 | 0.727 | 0.273 | 0.231 | 3 |  |
| -0.4963 | 12 | 0.667 | 0.250 | 0.231 | 4 |  |
| -0.6096 | 13 | 0.692 | 0.231 | 0.231 | 4 |  |
| -0.6183 | 14 | 0.643 | 0.214 | 0.231 | 5 |  |
| -0.6191 | 15 | 0.667 | 0.267 | 0.308 | 5 |  |
| -0.6848 | 16 | 0.625 | 0.250 | 0.308 | 6 |  |
| -0.6949 | 17 | 0.647 | 0.294 | 0.385 | 6 |  |
| -0.7549 | 18 | 0.611 | 0.278 | 0.385 | 7 |  |
| -0.7625 | 19 | 0.579 | 0.263 | 0.385 | 8 |  |
| -0.7633 | 20 | 0.600 | 0.250 | 0.385 | 8 |  |
| -0.8404 | 21 | 0.619 | 0.238 | 0.385 | 8 |  |
| -0.8866 | 22 | 0.636 | 0.227 | 0.385 | 8 |  |
| -0.9403 | 23 | 0.652 | 0.261 | 0.462 | 8 |  |
| -1.0443 | 24 | 0.625 | 0.250 | 0.462 | 9 |  |
| -1.0515 | 25 | 0.600 | 0.240 | 0.462 | 10 |  |
| -1.1887 | 26 | 0.615 | 0.231 | 0.462 | 10 |  |
| -1.2561 | 27 | 0.593 | 0.222 | 0.462 | 11 |  |
| -1.3236 | 28 | 0.571 | 0.214 | 0.462 | 12 |  |
| -1.3689 | 29 | 0.586 | 0.241 | 0.538 | 12 |  |
| -1.3958 | 30 | 0.567 | 0.233 | 0.538 | 13 |  |
| -1.4193 | 31 | 0.548 | 0.226 | 0.538 | 14 |  |
| -1.4792 | 32 | 0.562 | 0.250 | 0.615 | 14 |  |
| -1.5065 | 33 | 0.576 | 0.273 | 0.692 | 14 |  |
| -1.5337 | 34 | 0.588 | 0.265 | 0.692 | 14 |  |
| -1.6542 | 35 | 0.600 | 0.257 | 0.692 | 14 |  |
| -1.7245 | 36 | 0.583 | 0.250 | 0.692 | 15 |  |
| -1.7331 | 37 | 0.595 | 0.243 | 0.692 | 15 |  |
| -1.7358 | 38 | 0.605 | 0.263 | 0.769 | 15 |  |
| -1.7783 | 39 | 0.590 | 0.256 | 0.769 | 16 |  |
| -1.9041 | 40 | 0.600 | 0.250 | 0.769 | 16 |  |
| -1.9391 | 41 | 0.610 | 0.244 | 0.769 | 16 |  |
| -1.9547 | 42 | 0.619 | 0.238 | 0.769 | 16 | **chosen** |
| -2.0477 | 43 | 0.605 | 0.233 | 0.769 | 17 |  |
| -2.1002 | 44 | 0.591 | 0.227 | 0.769 | 18 |  |
| -2.1006 | 45 | 0.600 | 0.244 | 0.846 | 18 |  |
| -2.1124 | 46 | 0.587 | 0.239 | 0.846 | 19 |  |
| -2.1653 | 47 | 0.574 | 0.234 | 0.846 | 20 |  |
| -2.1656 | 48 | 0.583 | 0.250 | 0.923 | 20 |  |
| -2.1806 | 49 | 0.571 | 0.245 | 0.923 | 21 |  |
| -2.2612 | 50 | 0.560 | 0.240 | 0.923 | 22 |  |
| -2.3946 | 51 | 0.569 | 0.235 | 0.923 | 22 |  |
| -2.4066 | 52 | 0.558 | 0.231 | 0.923 | 23 |  |
| -2.5018 | 53 | 0.547 | 0.226 | 0.923 | 24 |  |
| -2.5268 | 54 | 0.556 | 0.222 | 0.923 | 24 |  |
| -2.5477 | 55 | 0.545 | 0.218 | 0.923 | 25 |  |
| -2.5742 | 56 | 0.536 | 0.214 | 0.923 | 26 |  |
| -2.6519 | 57 | 0.526 | 0.211 | 0.923 | 27 |  |
| -2.7039 | 58 | 0.517 | 0.207 | 0.923 | 28 |  |
| -2.7147 | 59 | 0.508 | 0.203 | 0.923 | 29 |  |
| -2.7152 | 60 | 0.517 | 0.217 | 1.000 | 29 |  |
| -2.8373 | 61 | 0.525 | 0.213 | 1.000 | 29 |  |
| -2.8381 | 62 | 0.516 | 0.210 | 1.000 | 30 |  |
| -2.8861 | 63 | 0.508 | 0.206 | 1.000 | 31 |  |
| -2.9362 | 64 | 0.516 | 0.203 | 1.000 | 31 |  |
| -2.9852 | 65 | 0.523 | 0.200 | 1.000 | 31 |  |
| -3.2618 | 66 | 0.515 | 0.197 | 1.000 | 32 |  |
| -3.3479 | 67 | 0.507 | 0.194 | 1.000 | 33 |  |
| -3.3711 | 68 | 0.500 | 0.191 | 1.000 | 34 |  |
| -3.3976 | 69 | 0.493 | 0.188 | 1.000 | 35 |  |
| -3.5625 | 70 | 0.486 | 0.186 | 1.000 | 36 |  |
| -3.6590 | 71 | 0.479 | 0.183 | 1.000 | 37 |  |
| -3.6602 | 72 | 0.472 | 0.181 | 1.000 | 38 |  |
| -3.7132 | 73 | 0.479 | 0.178 | 1.000 | 38 |  |
| -3.8342 | 74 | 0.473 | 0.176 | 1.000 | 39 |  |
| -4.0544 | 75 | 0.467 | 0.173 | 1.000 | 40 |  |
| -4.3238 | 76 | 0.461 | 0.171 | 1.000 | 41 |  |
<!-- rerank-gate:end -->

### Reading the rerank-gate table (2026-09-04)

The block above is regenerated by `calibrate_rerank_gate.py --write-results`;
everything below it is hand-written and survives a re-run.

**The gate does not pass its acceptance bar on this labelled set, and should
stay off (`auto_recall_min_rerank = 0.0`) until it does.** No threshold
reaches precision >= 0.65 while keeping relevance-2 recall >= 0.7. Reading
down the curve, precision peaks around 0.83 at 6 injected pairs — but that
point recalls only 23% of the directly-relevant memories, so the gate would
be buying precision by muting recall almost entirely.

**Cross-encoder scores are raw logits, not probabilities.** The observed
range for `jinaai/jina-reranker-v1-turbo-en` is **-4.32 to +0.87**; almost
every real pair scores negative. A "sensible-looking" threshold such as 0.5
would inject roughly one document in seventy-six. Any threshold copied from
another model, or guessed from the 0-1 range the old `query_hybrid_rerank`
docstring claimed, is meaningless. The stats planner's `x_rerank_scores`
buckets of (0.1, 0.3, 0.5, 0.7) do not fit this range and need retuning to
roughly (-2.5, -1.5, -0.75, 0.0).

**Model comparison** (same 76 pairs, same `--min-recall2 0.7`):

| model | chosen t | injected | precision (rel>=1) | precision (rel==2) | recall (rel==2) | harmful |
|---|---:|---:|---:|---:|---:|---:|
| `jinaai/jina-reranker-v1-turbo-en` (config default) | -1.9547 | 42 | 0.619 | 0.238 | 0.769 | 16 |
| `Xenova/ms-marco-MiniLM-L-6-v2` | -6.9624 | 36 | 0.639 | 0.333 | 0.923 | 13 |

MiniLM-L-6 is better on every axis here: higher precision, much higher
strict precision, higher relevance-2 recall, fewer harmful injections — and
it is half the size (0.08 GB vs 0.15 GB). Neither clears the bar.

**Rerank latency** (this machine, Apple silicon, weights warm in
`~/.cache/fastembed`, 20 real memories truncated to `RERANK_TEXT_CAP = 2000`
chars, 9 timed runs after a warm-up). The box was under heavy parallel load
(1-minute load average ~61) during the second run, so treat the `min` column
as the closest thing to an idle-machine number and the p50 as a
contended-machine number:

| model | candidates | p50 (run 1 / run 2) | min | max |
|---|---:|---:|---:|---:|
| jina-reranker-v1-turbo-en | 20 | 553 / 983 ms | 466 ms | 1363 ms |
| jina-reranker-v1-turbo-en | 10 | 413 / 521 ms | 323 ms | 756 ms |
| ms-marco-MiniLM-L-6-v2 | 20 | 731 / 1075 ms | 380 ms | 2085 ms |
| ms-marco-MiniLM-L-6-v2 | 10 | 284 / 227 ms | 192 ms | 617 ms |

Model load from warm cache is 0.2-0.5 s, paid once at daemon start.

**This does not fit the 800 ms hook budget at `rerank_n = 20`.** The rerank
stage alone spends 0.47-1.4 s there, before Qdrant retrieval, IPC or
rendering. `rerank_n = 10` on MiniLM-L-6 (0.19-0.62 s) is the only measured
combination with real headroom.

### Labelled set: provenance and limits

76 pairs over 38 distinct prompts, 13 of them relevance-2.

- `graded16` (46 pairs): the 16 graded prompts from the 2026-09-04 retrieval
  audit, `rerun_top3` entries resolved from the judge's document `name` to a
  unique file under `~/.agent/memory` / `~/.agent/imports` (exact filename
  stem, exact frontmatter `name`/H1, digest `<slug>__<hash>` prefix, then a
  0.86-cutoff fuzzy match, with path fragments used to break ties). All 48
  entries resolved; 0 remain unresolved.
- `util24` (18) and `util24-multi` (12): the 2026-09-04 utilization sample.
- Two `(prompt, path)` pairs were labelled twice — once under
  `util24-multi` and once under `graded16` — with identical relevance. The
  `graded16` copies were dropped so each judgment counts once (78 -> 76).
- `rq30` (30 prompts / 90 judgments from 2026-08-05) is **unavailable**: the
  scratchpad `judgments.json` no longer exists and the report kept only
  aggregates.

The set is small and skewed toward negatives, because it is drawn from what
recall actually injected and a judge then graded. That is the honest
population for a precision gate, but 13 relevance-2 pairs make the
recall >= 0.7 constraint coarse: it moves in steps of ~0.077. Revisit after
two weeks of v1.2 telemetry, where `x_rerank_scores` on misses supplies free
negatives.
