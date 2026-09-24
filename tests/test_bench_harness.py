"""Tests for the benchmark harness extensions (Phase 1).

Pure-function coverage only: dataset conversion (LongMemEval v1 + v2), stale
/contradiction metric math, result-block writing, and dataset dispatch. The
heavy end-to-end retrieval path is marked `embeddings` and never runs in CI.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.bench_recall_ab import (
    aggregate_metrics,
    compute_rank_row,
    coverage,
    load_dataset,
    write_results_block,
)
from eval.longmemeval_convert import convert_v1, convert_v2


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _v1_instances() -> list[dict]:
    return [
        {
            "question_id": "q1",
            "question_type": "single-session-user",
            "question": "What did I study?",
            "answer": "Business Administration",
            "question_date": "2023/05/01",
            "haystack_session_ids": ["s1", "s2"],
            "answer_session_ids": ["s2"],
            "haystack_dates": ["2023/04/01", "2023/04/02"],
            "haystack_sessions": [
                [{"role": "user", "content": "I like pasta"}],
                [
                    {"role": "user", "content": "I studied Business Administration"},
                    {"role": "assistant", "content": "Noted."},
                ],
            ],
        },
        {
            "question_id": "q2",
            "question_type": "knowledge-update",
            "question": "What framework do I use?",
            "answer": "FastAPI",
            "question_date": "2023/06/01",
            "haystack_session_ids": ["s3"],
            "answer_session_ids": ["s3"],
            "haystack_dates": ["2023/05/02"],
            "haystack_sessions": [
                [{"role": "user", "content": "I switched from Flask to FastAPI"}],
            ],
        },
    ]


def _write_v2_root(root: Path) -> Path:
    (root / "haystacks").mkdir(parents=True, exist_ok=True)
    (root / "questions.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "id": "vq1",
                        "domain": "web",
                        "environment": "shop",
                        "question_type": "single-session-user",
                        "question": "What price did I see?",
                        "image": None,
                        "answer": "$42",
                        "eval_function": "exact",
                    }
                )
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "trajectories.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "id": "t1",
                        "domain": "web",
                        "environment": "shop",
                        "goal": "buy shoes",
                        "outcome": "success",
                        "start_url": "https://shop.example",
                        "states": [
                            {
                                "state_index": 0,
                                "step": 0,
                                "url": "https://shop.example",
                                "action": None,
                                "thought": "looking for shoes",
                                "accessibility_tree": "button 'buy'  text '$42'",
                            }
                        ],
                    }
                ),
                json.dumps(
                    {
                        "id": "t2",
                        "domain": "web",
                        "environment": "shop",
                        "goal": "compare hats",
                        "outcome": "failure",
                        "start_url": "https://shop.example/hats",
                        "states": [],
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "haystacks" / "lme_v2_small.json").write_text(
        json.dumps({"vq1": ["t1", "t2"]}), encoding="utf-8"
    )
    return root


# ---------------------------------------------------------------------------
# v1 conversion
# ---------------------------------------------------------------------------

def test_convert_v1_shape_and_supports():
    native = convert_v1(_v1_instances())
    assert set(native) == {"corpus", "questions"}
    slug2doc = {d["slug"]: d for d in native["corpus"]}
    # 3 sessions total across both instances, slugs are unique and stable
    assert len(slug2doc) == 3
    q1 = next(q for q in native["questions"] if q["id"] == "q1")
    # supports reference corpus slugs containing the answer session id
    assert len(q1["supports"]) == 1
    assert "s2" in q1["supports"][0]
    assert q1["answer_substring"] == "Business Administration"
    # retrieval is restricted to this question's own haystack
    assert sorted(q1["docs"]) == sorted(d["slug"] for d in native["corpus"] if "q1" in d["slug"])
    # session body preserves speaker turns
    doc = slug2doc[q1["supports"][0]]
    assert "Business Administration" in doc["body"]
    assert "user" in doc["body"]


def test_convert_v1_question_metadata_kept():
    native = convert_v1(_v1_instances())
    q2 = next(q for q in native["questions"] if q["id"] == "q2")
    assert q2["question_type"] == "knowledge-update"
    assert q2["q"] == "What framework do I use?"


def test_convert_v1_rejects_bad_instances():
    with pytest.raises(KeyError):
        convert_v1([{"question_id": "broken"}])


# ---------------------------------------------------------------------------
# v2 conversion
# ---------------------------------------------------------------------------

def test_convert_v2_shape(tmp_path):
    root = _write_v2_root(tmp_path / "v2")
    native = convert_v2(root, tier="small")
    slugs = {d["slug"] for d in native["corpus"]}
    assert slugs == {"t1", "t2"}
    q = native["questions"][0]
    assert q["id"] == "vq1"
    assert q["q"] == "What price did I see?"
    assert q["answer_substring"] == "$42"
    # v2 haystack gives no answer-trajectory link: coverage-only question
    assert q["supports"] == []
    assert q["docs"] == ["t1", "t2"]
    # trajectory body flattens goal + thought + accessibility tree text
    t1 = next(d for d in native["corpus"] if d["slug"] == "t1")
    assert "buy shoes" in t1["body"]
    assert "$42" in t1["body"]


def test_convert_v2_missing_files_raise(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        convert_v2(empty, tier="small")


def test_convert_v2_dangling_haystack_reference_raises(tmp_path):
    root = _write_v2_root(tmp_path / "v2")
    (root / "haystacks" / "lme_v2_small.json").write_text(
        json.dumps({"vq1": ["t1", "ghost"]}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="ghost"):
        convert_v2(root, tier="small")


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

def test_load_dataset_native_passthrough(tmp_path):
    native = {"corpus": [{"slug": "a", "body": "x"}], "questions": []}
    p = tmp_path / "native.json"
    p.write_text(json.dumps(native), encoding="utf-8")
    assert load_dataset(p) == native


def test_load_dataset_v1_detected(tmp_path):
    p = tmp_path / "lme.json"
    p.write_text(json.dumps(_v1_instances()), encoding="utf-8")
    native = load_dataset(p)
    assert any("s2" in d["slug"] for d in native["corpus"])


def test_load_dataset_v2_directory(tmp_path):
    root = _write_v2_root(tmp_path / "v2")
    native = load_dataset(root)
    assert native["questions"][0]["id"] == "vq1"


def test_load_dataset_rejects_garbage(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"nope": 1}), encoding="utf-8")
    with pytest.raises(SystemExit):
        load_dataset(p)


# ---------------------------------------------------------------------------
# metric math (pure)
# ---------------------------------------------------------------------------

def test_compute_rank_row_support_and_stale():
    row = compute_rank_row(
        ranked_slugs=["stale1", "cur1", "other"],
        top_text="old fact new fact",
        item={
            "supports": ["cur1"],
            "answer_substring": "new fact",
            "stale": ["stale1"],
            "stale_answer_substring": "old fact",
        },
    )
    assert row["first_support_rank"] == 2
    assert row["first_stale_rank"] == 1
    assert row["stale_ahead"] is True
    assert row["stale_solo"] is False
    assert row["answer_covered"] is True
    assert row["stale_answer_covered"] is True


def test_compute_rank_row_stale_solo_when_support_absent():
    """Stale doc retrieved while the current one is missed is a distinct
    failure mode (retrieval miss), not a strict outranking."""
    row = compute_rank_row(
        ranked_slugs=["stale1", "other"],
        top_text="old fact",
        item={
            "supports": ["cur1"],
            "answer_substring": "new fact",
            "stale": ["stale1"],
            "stale_answer_substring": "old fact",
        },
    )
    assert row["first_support_rank"] == 0
    assert row["stale_ahead"] is False
    assert row["stale_solo"] is True


def test_compute_rank_row_current_ahead():
    row = compute_rank_row(
        ranked_slugs=["cur1", "stale1"],
        top_text="new fact",
        item={"supports": ["cur1"], "answer_substring": "new fact", "stale": ["stale1"]},
    )
    assert row["first_support_rank"] == 1
    assert row["stale_ahead"] is False
    assert row["stale_solo"] is False
    assert row["stale_answer_covered"] is False


def test_compute_rank_row_no_labels():
    row = compute_rank_row(
        ranked_slugs=["a", "b"],
        top_text="hello",
        item={"supports": [], "answer_substring": "hello"},
    )
    assert row["first_support_rank"] == 0
    assert row["first_stale_rank"] == 0
    assert row["stale_ahead"] is None
    assert row["stale_solo"] is None
    assert row["answer_covered"] is True


def test_coverage_case_insensitive():
    assert coverage("Business Administration", "business administration") is True
    assert coverage("x", "") is False


def test_coverage_coerces_non_string_answer():
    """Real LongMemEval answers are not always strings (numeric → int)."""
    assert coverage("I have 42 packages", 42) is True
    assert coverage("nothing here", None) is False


def test_convert_v1_coerces_int_answer():
    inst = _v1_instances()[0]
    inst["answer"] = 42
    native = convert_v1([inst])
    assert native["questions"][0]["answer_substring"] == "42"


def test_aggregate_metrics_scopes_recall_to_supported_questions():
    rows = [
        {"id": "q1", "question_type": "a", "first_support_rank": 1, "first_stale_rank": 0,
         "answer_covered": True, "stale_ahead": None, "stale_solo": None,
         "stale_answer_covered": False, "has_supports": True},
        {"id": "q2", "question_type": "a", "first_support_rank": 0, "first_stale_rank": 0,
         "answer_covered": True, "stale_ahead": None, "stale_solo": None,
         "stale_answer_covered": False, "has_supports": False},
        {"id": "q3", "question_type": "b", "first_support_rank": 0, "first_stale_rank": 2,
         "answer_covered": False, "stale_ahead": False, "stale_solo": True,
         "stale_answer_covered": True, "has_supports": True},
        {"id": "q4", "question_type": "b", "first_support_rank": 3, "first_stale_rank": 1,
         "answer_covered": True, "stale_ahead": True, "stale_solo": False,
         "stale_answer_covered": True, "has_supports": True},
    ]
    m = aggregate_metrics(rows, n_docs=4)
    # recall@1 only over the 3 questions that have supports (q1 hits, q3/q4 miss@1)
    assert m["recall@1"] == round(1 / 3, 3)
    assert m["n_questions"] == 4
    assert m["n_with_supports"] == 3
    # coverage over all 4 questions (3 of 4 covered)
    assert m["answer_coverage@5"] == round(3 / 4, 3)
    # strict stale-ahead: only q4 has both docs retrieved → 1/1
    assert m["stale_ahead_rate"] == 1.0
    # solo: q3 of the 2 stale-labelled → 0.5
    assert m["stale_solo_rate"] == 0.5
    # contamination: q3 and q4 both lead with stale → 1.0
    assert m["stale_contamination_rate"] == 1.0
    assert m["stale_answer_coverage@5"] == 1.0
    # by_type breakdown
    assert m["by_type"]["a"]["n"] == 2
    assert m["by_type"]["a"]["answer_coverage@5"] == 1.0
    assert m["by_type"]["b"]["recall@5"] == 0.5
    assert m["by_type"]["b"]["answer_coverage@5"] == 0.5


# ---------------------------------------------------------------------------
# results block writer
# ---------------------------------------------------------------------------

def test_write_results_block_replaces_between_markers(tmp_path):
    md = tmp_path / "RESULTS.md"
    md.write_text(
        "before\n<!-- bench-ab:start -->\nOLD\n<!-- bench-ab:end -->\nafter\n",
        encoding="utf-8",
    )
    write_results_block(md, "NEW NUMBERS")
    text = md.read_text(encoding="utf-8")
    assert "OLD" not in text
    assert "NEW NUMBERS" in text
    assert text.startswith("before\n")
    assert text.endswith("after\n")
    # idempotent: second write replaces, never duplicates
    write_results_block(md, "NEWER")
    text2 = md.read_text(encoding="utf-8")
    assert "NEW NUMBERS" not in text2
    assert text2.count("<!-- bench-ab:start -->") == 1


def test_write_results_block_appends_when_markers_missing(tmp_path):
    md = tmp_path / "RESULTS.md"
    md.write_text("no markers here\n", encoding="utf-8")
    write_results_block(md, "BLOCK")
    text = md.read_text(encoding="utf-8")
    assert "no markers here" in text
    assert "<!-- bench-ab:start -->" in text and "BLOCK" in text


def test_write_results_block_empty_file_has_no_leading_blank(tmp_path):
    md = tmp_path / "RESULTS.md"
    write_results_block(md, "BLOCK")
    assert md.read_text(encoding="utf-8").startswith("<!-- bench-ab:start -->")


# ---------------------------------------------------------------------------
# shipped datasets are well-formed (hermetic — no embeddings)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "dataset",
    ["bench_dataset.json", "bench_dataset_contradictions.json"],
)
def test_shipped_datasets_are_well_formed(dataset):
    native = load_dataset(Path(__file__).parent.parent / "eval" / dataset)
    slugs = {d["slug"] for d in native["corpus"]}
    assert len(slugs) == len(native["corpus"]), "duplicate corpus slugs"
    for q in native["questions"]:
        for ref in q.get("supports", []) + q.get("stale", []) + q.get("docs", []):
            assert ref in slugs, f"{q.get('id')}: dangling reference {ref}"
        assert q.get("answer_substring"), f"{q.get('id')}: empty answer"


def test_run_benchmark_empty_corpus_exits_cleanly():
    from eval.bench_recall_ab import run_benchmark

    with pytest.raises(SystemExit, match="empty"):
        run_benchmark({"corpus": [], "questions": [{"q": "x"}]})


def test_run_benchmark_limit_zero_builds_no_retriever():
    """limit=0 short-circuits before any retriever/model construction, so it
    runs hermetically (no embeddings download) even in CI."""
    from eval.bench_recall_ab import run_benchmark

    native = {
        "corpus": [{"slug": "a", "body": "hello world"}],
        "questions": [{"q": "hello", "supports": ["a"], "answer_substring": "hello"}],
    }
    # limit=0 is falsy → run everything; use an explicit zero-question set
    # instead: same no-retriever path.
    result = run_benchmark({"corpus": native["corpus"], "questions": []})
    assert result["n_questions"] == 0
    assert result["recall@5"] == 0.0


# ---------------------------------------------------------------------------
# sharding + merging (pure — no retriever)
# ---------------------------------------------------------------------------

def test_merge_shard_results_recomputes_aggregates():
    from eval.bench_recall_ab import merge_shard_results

    shard0 = {
        "n_docs": 4,
        "per_question": [
            {"id": "q0", "question_type": "a", "first_support_rank": 1, "first_stale_rank": 0,
             "answer_covered": True, "stale_ahead": None, "stale_solo": None,
             "stale_answer_covered": False, "has_supports": True},
        ],
    }
    shard1 = {
        "n_docs": 4,
        "per_question": [
            {"id": "q1", "question_type": "a", "first_support_rank": 0, "first_stale_rank": 0,
             "answer_covered": False, "stale_ahead": None, "stale_solo": None,
             "stale_answer_covered": False, "has_supports": True},
        ],
    }
    merged = merge_shard_results([shard0, shard1])
    assert merged["n_questions"] == 2
    assert merged["recall@1"] == 0.5
    assert merged["answer_coverage@5"] == 0.5
    assert merged["n_shards"] == 2
    # identical to a single-process aggregate over the same rows
    assert merged["recall@5"] == aggregate_metrics(
        shard0["per_question"] + shard1["per_question"], n_docs=4
    )["recall@5"]


# ---------------------------------------------------------------------------
# end-to-end on the shipped contradiction set (heavy — never in CI)
# ---------------------------------------------------------------------------

@pytest.mark.embeddings
def test_contradiction_dataset_end_to_end(tmp_path, monkeypatch):
    """Runs the real retriever over the contradiction dataset: the dataset→
    metrics path is pinned, and pre-Phase-2 the corpus has no temporal
    frontmatter so stale contamination must be visible (> 0)."""
    from eval.bench_recall_ab import run_benchmark

    monkeypatch.setenv(
        "FASTEMBED_CACHE_PATH", str(Path.home() / ".cache" / "fastembed")
    )
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    native = load_dataset(
        Path(__file__).parent.parent / "eval" / "bench_dataset_contradictions.json"
    )
    result = run_benchmark(native)
    assert result["n_questions"] == 8
    assert "stale_contamination_rate" in result
    assert result["recall@5"] == 1.0
    # Pre-Phase-2 baseline: no temporal demotion exists, so stale docs lead
    # on a measurable fraction of contradiction questions.
    assert result["stale_contamination_rate"] > 0.0


@pytest.mark.embeddings
def test_per_question_haystacks_are_isolated(tmp_path, monkeypatch):
    """Regression for the shared-store accumulation bug: two questions with
    disjoint haystacks must never see each other's docs. (All retrievers in
    one run share one on-disk Qdrant store via XDG_CACHE_HOME.)"""
    from eval.bench_recall_ab import run_benchmark

    monkeypatch.setenv(
        "FASTEMBED_CACHE_PATH", str(Path.home() / ".cache" / "fastembed")
    )
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    native = {
        "corpus": [
            {"slug": "a1", "body": "alpha project deploys with Ansible"},
            {"slug": "a2", "body": "alpha project databases are Postgres"},
            {"slug": "b1", "body": "beta bakery sells sourdough bread"},
            {"slug": "b2", "body": "beta bakery opens at seven"},
        ],
        "questions": [
            {"id": "qa", "q": "ansible deploys", "supports": ["a1"],
             "answer_substring": "Ansible", "docs": ["a1", "a2"]},
            {"id": "qb", "q": "sourdough", "supports": ["b1"],
             "answer_substring": "sourdough", "docs": ["b1", "b2"]},
        ],
    }
    result = run_benchmark(native)
    for row, item in zip(result["per_question"], native["questions"]):
        assert row["first_support_rank"] == 1, f"leakage or miss on {row['id']}"
        # Hard isolation check: nothing from the other question's haystack
        # may appear in this question's ranking at all.
        assert set(row["ranked"]) <= set(item["docs"]), (
            f"{row['id']}: cross-haystack leakage: {row['ranked']}"
        )
