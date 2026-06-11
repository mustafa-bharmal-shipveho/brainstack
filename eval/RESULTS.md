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
