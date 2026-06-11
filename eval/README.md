# Evaluation harnesses

## `bench_recall_ab.py`: does recall surface the right memory?

A reproducible auto-recall A/B. The honest precondition for "memory makes the
agent better" is "memory surfaces the right note when asked," and that is what
this measures, on a labeled set, comparing:

- **A (no memory):** an empty brain surfaces nothing, so every memory-grounded
  metric is 0 by construction.
- **B (with recall):** a `HybridRetriever` over the labeled corpus, queried
  with each question.

```bash
make bench                 # ships a labeled synthetic set (16 docs, 21 questions)
python eval/bench_recall_ab.py --json
python eval/bench_recall_ab.py --dataset path/to/longmemeval.json
```

Metrics (condition B): recall@1/3/5, MRR, and answer-coverage@5 (the fraction
of questions whose answer text lands in the top-5 context, a proxy for
"re-explanation avoided"). Latest numbers and methodology are in
[`RESULTS.md`](RESULTS.md).

### Honest scope

This is a **retrieval-grounded** benchmark, not an end-to-end task-success
score with an LLM judge. It proves the right memory is surfaced; it does not by
itself prove the agent's final answer is better. The shipped set is synthetic
and small, with distractor documents and indirect phrasing added so the score
is not a trivial 1.000. The credible public benchmark is **LongMemEval**
(multi-session, with distractors); the harness ingests its JSON via
`--dataset`, but running the full public set is tracked in `ROADMAP.md`, not
done here.

## `auto_recall_harness.py`: manual side-by-side grading

Generates prompt pairs (with/without injected context) for human grading. See
its module docstring.

## `load_test_locking.py`

Stress test for the brain lock under concurrent writers.
