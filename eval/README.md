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
python eval/bench_recall_ab.py --dataset eval/data/longmemeval_s_cleaned.json
python eval/bench_recall_ab.py --dataset eval/bench_dataset_contradictions.json
python eval/bench_recall_ab.py --dataset eval/data/lme_v2   # LongMemEval-V2 root
python eval/bench_recall_ab.py --dataset X --limit 50       # cheap smoke of a big set
```

Metrics (condition B): recall@1/3/5 and MRR over questions with `supports`
labels; answer-coverage@5 over all questions (the "re-explanation avoided" /
retrieval-sufficiency proxy); and, on contradiction-labelled sets such as
`bench_dataset_contradictions.json`, stale-ahead rate (an outdated doc
outranks the current one) and stale-answer-coverage@5 (the outdated answer
text reaches the injected context). Latest numbers and methodology are in
[`RESULTS.md`](RESULTS.md); `--write-results` refreshes the marked block.

`--dataset` accepts three shapes (conversion in `longmemeval_convert.py`):
native `{corpus, questions}` JSON, LongMemEval v1 JSON (each session becomes
one doc; each question retrieves within its own haystack), or a prepared
LongMemEval-V2 data directory (`questions.jsonl` + `trajectories.jsonl` +
`haystacks/lme_v2_<tier>.json`; V2 has no answer-trajectory labels, so its
questions are coverage-only). Small labeled sets ship in `eval/`; large
downloaded benchmarks live in `eval/data/`, which is gitignored — download
LongMemEval-S from
`https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned` and V2 from
`https://huggingface.co/datasets/xiaowu0162/longmemeval-v2`.

### Honest scope

This is a **retrieval-grounded** benchmark, not an end-to-end task-success
score with an LLM judge. It proves the right memory is surfaced; it does not by
itself prove the agent's final answer is better. The shipped set is synthetic
and small, with distractor documents and indirect phrasing added so the score
is not a trivial 1.000. The same code path runs the public **LongMemEval-S**
(500 questions, per-question haystacks) and **LongMemEval-V2** (451 questions,
coverage-only) sets; per-run numbers and caveats live in
[`RESULTS.md`](RESULTS.md).

## `calibrate_rerank_gate.py`: what should `auto_recall_min_rerank` be?

Auto-recall can refuse to inject a memory whose cross-encoder score is below
`auto_recall_min_rerank`. That threshold is **model-specific** — cross-encoder
outputs are raw logits, not probabilities — so it has to be measured, not
guessed. This script sweeps every distinct score in a labelled set and reports
precision, strict precision, relevance-2 recall and harmful (irrelevant)
injections at each candidate threshold.

```bash
# Heavy: loads a real cross-encoder. Never runs in `make test-ci`.
.venv/bin/python3 eval/calibrate_rerank_gate.py \
    --labels eval/labels/rerank_gate_labels.jsonl \
    --model jinaai/jina-reranker-v1-turbo-en \
    --min-recall2 0.7 --write-results \
    --curve-out eval/rerank_gate_curve.json

# Compare a second model (omit --write-results so it doesn't clobber the report)
.venv/bin/python3 eval/calibrate_rerank_gate.py \
    --labels eval/labels/rerank_gate_labels.jsonl \
    --model Xenova/ms-marco-MiniLM-L-6-v2 --min-recall2 0.7
```

Labels are JSONL, one `{"prompt", "path", "relevance", "set"}` per line, with
`relevance` in `{0: irrelevant, 1: tangential, 2: directly relevant}`. Each
pair is scored on the same text the daemon reranks:
`_build_indexed_text(name, description, body)` truncated to
`recall.qdrant_backend.RERANK_TEXT_CAP` (2000) chars. The chosen point is the
highest-precision threshold that still recalls at least `--min-recall2` of the
relevance-2 pairs; ties prefer the tighter threshold. `--write-results`
replaces the block between `<!-- rerank-gate:start -->` and
`<!-- rerank-gate:end -->` in [`RESULTS.md`](RESULTS.md); `--scores-cache PATH`
reuses scores so you can re-sweep without reloading the model.

Exit code: 0 when a viable threshold exists, 1 when none does, 2 on bad input.
Re-run it whenever `ranking.reranker_model` changes — an old threshold on a new
model is worse than no gate. Latest numbers, the model comparison and the
measured rerank latency are in [`RESULTS.md`](RESULTS.md).

## `auto_recall_harness.py`: manual side-by-side grading

Generates prompt pairs (with/without injected context) for human grading. See
its module docstring.

## `load_test_locking.py`

Stress test for the brain lock under concurrent writers.
