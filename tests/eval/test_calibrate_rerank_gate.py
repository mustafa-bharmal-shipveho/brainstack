"""Red-phase tests (S4): `eval/calibrate_rerank_gate.py`.

The calibrator turns a labelled `(prompt, path, relevance)` set into a
threshold for `auto_recall_min_rerank`. The heavy part (scoring pairs with a
real cross-encoder) is marked `embeddings`; everything else — label loading,
the indexed-text mirror, the sweep, the choice rule, the markdown render and
the marker-delimited write into `eval/RESULTS.md` — is pure and hermetic and
runs in `make test-ci`.

Planned contract (not implemented yet), from the S4 plan:

    @dataclass LabeledPair(prompt, path, relevance, set_name)
    @dataclass ScoredPair(pair, score)
    @dataclass CurvePoint(threshold, n_injected, precision_rel,
                          precision_strict, recall2, harmful_injected)

    load_labels(path) -> list[LabeledPair]
    indexed_text_for_file(path) -> str
    make_scorer(model) -> Callable[[str, list[str]], list[float]]
    score_pairs(pairs, scorer, *, text_cap=None) -> list[ScoredPair]
        (text_cap=None means recall.qdrant_backend.RERANK_TEXT_CAP)
    sweep(scored) -> list[CurvePoint]
    choose(curve, *, min_recall2=0.7) -> CurvePoint | None
    render_markdown(curve, chosen, *, model, n_pairs, sets, date) -> str
    write_results(md, results_path) -> None

The real labelled set lives outside the repo at
`~/Documents/brainstack-audit-2026-09-04/labels/rerank_gate_labels.jsonl`
(52 lines, schema `{"prompt","path","relevance","set","used"}` — note the
extra `used` key, which `load_labels` must tolerate). These tests never read
it: every fixture writes its own jsonl under `tmp_path`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import eval.calibrate_rerank_gate as cg  # noqa: E402

START = "<!-- rerank-gate:start -->"
END = "<!-- rerank-gate:end -->"
DEFAULT_MODEL = "jinaai/jina-reranker-v1-turbo-en"


def _pair(
    prompt: str = "q", path: str = "/synth/a.md", relevance: int = 1, set_name: str = "synthetic"
) -> "cg.LabeledPair":
    return cg.LabeledPair(
        prompt=prompt, path=path, relevance=relevance, set_name=set_name
    )


def _scored(relevance: int, score: float) -> "cg.ScoredPair":
    return cg.ScoredPair(
        pair=_pair(path=f"/synth/rel{relevance}-{score}.md", relevance=relevance),
        score=score,
    )


def _point(
    threshold: float,
    *,
    precision_rel: float,
    recall2: float,
    precision_strict: float | None = None,
    n_injected: int = 10,
    harmful_injected: int = 0,
) -> "cg.CurvePoint":
    return cg.CurvePoint(
        threshold=threshold,
        n_injected=n_injected,
        precision_rel=precision_rel,
        # Kept co-monotone with precision_rel on purpose: the choice must
        # come out the same whichever precision the implementation maximises.
        precision_strict=precision_rel if precision_strict is None else precision_strict,
        recall2=recall2,
        harmful_injected=harmful_injected,
    )


def _write_labels(path: Path, rows: list[dict]) -> Path:
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    return path


# ---------------------------------------------------------------------------
# load_labels
# ---------------------------------------------------------------------------


class TestLoadLabels:
    def test_parses_rows_and_maps_set_to_set_name(self, tmp_path):
        p = _write_labels(
            tmp_path / "labels.jsonl",
            [
                {
                    "prompt": "why is production on fire",
                    "path": "/brain/incident-runbook.md",
                    "relevance": 2,
                    "set": "util24",
                    # Extra keys exist in the real file and must be tolerated.
                    "used": 0,
                },
                {
                    "prompt": "how do releases get tagged",
                    "path": "/brain/release-notes.md",
                    "relevance": 0,
                    "set": "graded16",
                },
            ],
        )
        pairs = cg.load_labels(p)
        assert len(pairs) == 2
        assert pairs[0].prompt == "why is production on fire"
        assert pairs[0].path == "/brain/incident-runbook.md"
        assert pairs[0].relevance == 2
        assert pairs[0].set_name == "util24"
        assert pairs[1].set_name == "graded16"

    def test_blank_lines_are_skipped(self, tmp_path):
        p = tmp_path / "labels.jsonl"
        row = json.dumps(
            {"prompt": "q", "path": "/a.md", "relevance": 1, "set": "s"}
        )
        p.write_text(f"\n{row}\n\n   \n{row}\n", encoding="utf-8")
        assert len(cg.load_labels(p)) == 2

    @pytest.mark.parametrize("relevance", [3, -1, 1.5, "2", None])
    def test_rejects_relevance_outside_0_1_2(self, tmp_path, relevance):
        p = _write_labels(
            tmp_path / "labels.jsonl",
            [{"prompt": "q", "path": "/a.md", "relevance": relevance, "set": "s"}],
        )
        with pytest.raises(ValueError):
            cg.load_labels(p)

    @pytest.mark.parametrize("missing", ["prompt", "path", "relevance", "set"])
    def test_rejects_row_missing_a_required_key(self, tmp_path, missing):
        row = {"prompt": "q", "path": "/a.md", "relevance": 1, "set": "s"}
        row.pop(missing)
        p = _write_labels(tmp_path / "labels.jsonl", [row])
        with pytest.raises(ValueError):
            cg.load_labels(p)

    def test_rejects_malformed_json_line(self, tmp_path):
        p = tmp_path / "labels.jsonl"
        p.write_text('{"prompt": "q", "path"\n', encoding="utf-8")
        with pytest.raises(ValueError):
            cg.load_labels(p)


# ---------------------------------------------------------------------------
# indexed_text_for_file
# ---------------------------------------------------------------------------


class TestIndexedTextForFile:
    """The calibrator must score the SAME text the daemon reranks.

    `recall.sources.discover_documents` indexes
    `_build_indexed_text(name, description, body)` with the description
    repeated 3x. Scoring anything else calibrates a threshold for a text the
    reranker will never see.
    """

    def test_mirrors_recall_sources_build_indexed_text(self, tmp_path):
        from recall.frontmatter import parse_path
        from recall.sources import _build_indexed_text

        p = tmp_path / "incident-runbook.md"
        p.write_text(
            "---\n"
            "name: incident-runbook\n"
            "description: what to do when production goes down at 2am\n"
            "type: reference\n"
            "---\n"
            "Paste the rollback SQL inline first, then page the on-call.\n",
            encoding="utf-8",
        )

        text = cg.indexed_text_for_file(p)
        parsed = parse_path(p)
        assert text == _build_indexed_text(
            "incident-runbook",
            "what to do when production goes down at 2am",
            parsed.body,
        )
        # The 3x description weighting is the whole point.
        assert text.count("what to do when production goes down at 2am") == 3
        assert text.startswith("incident-runbook")
        assert "rollback SQL" in text

    def test_name_falls_back_to_file_stem(self, tmp_path):
        p = tmp_path / "plain-note.md"
        p.write_text(
            "---\ndescription: only a description\n---\nbody text here\n",
            encoding="utf-8",
        )
        text = cg.indexed_text_for_file(p)
        assert text.startswith("plain-note")
        assert text.count("only a description") == 3

    def test_file_without_frontmatter_uses_stem_and_empty_description(self, tmp_path):
        from recall.frontmatter import parse_path
        from recall.sources import _build_indexed_text

        p = tmp_path / "note.md"
        p.write_text("just some markdown, no frontmatter\n", encoding="utf-8")
        parsed = parse_path(p)
        assert cg.indexed_text_for_file(p) == _build_indexed_text(
            "note", "", parsed.body
        )


# ---------------------------------------------------------------------------
# score_pairs
# ---------------------------------------------------------------------------


class TestScorePairs:
    def test_preserves_order_and_caps_text(self, tmp_path):
        seen_texts: list[str] = []

        def scorer(prompt: str, texts: list[str]) -> list[float]:
            seen_texts.extend(texts)
            return [float(len(t)) for t in texts]

        paths = []
        for i, filler in enumerate(("short body", "x" * 5000)):
            p = tmp_path / f"doc{i}.md"
            p.write_text(
                f"---\nname: doc{i}\ndescription: d{i}\n---\n{filler}\n",
                encoding="utf-8",
            )
            paths.append(p)

        pairs = [
            _pair(prompt="q1", path=str(paths[0]), relevance=2),
            _pair(prompt="q2", path=str(paths[1]), relevance=0),
        ]
        scored = cg.score_pairs(pairs, scorer, text_cap=100)

        assert [s.pair for s in scored] == pairs, "one ScoredPair per input, in order"
        assert seen_texts, "scorer was never called"
        assert all(len(t) <= 100 for t in seen_texts), "text_cap not applied"
        # The 5000-char doc is capped to exactly text_cap, so the fake
        # scorer (which returns len) reports 100.
        assert scored[1].score == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# sweep
# ---------------------------------------------------------------------------


class TestSweep:
    """Corpus used below (5 pairs, 2 of them relevance-2):

        score 0.9 -> rel 2
        score 0.7 -> rel 2
        score 0.5 -> rel 1
        score 0.3 -> rel 0
        score 0.1 -> rel 0
    """

    @pytest.fixture
    def scored(self) -> list:
        return [
            _scored(2, 0.9),
            _scored(2, 0.7),
            _scored(1, 0.5),
            _scored(0, 0.3),
            _scored(0, 0.1),
        ]

    def test_one_point_per_distinct_score(self, scored):
        curve = cg.sweep(scored)
        assert sorted(p.threshold for p in curve) == pytest.approx(
            [0.1, 0.3, 0.5, 0.7, 0.9]
        )

    def test_n_injected_is_monotone_as_threshold_decreases(self, scored):
        curve = sorted(cg.sweep(scored), key=lambda p: -p.threshold)
        counts = [p.n_injected for p in curve]
        assert counts == sorted(counts), f"not monotone: {counts}"
        assert counts[0] == 1 and counts[-1] == 5

    def test_metrics_at_a_mid_threshold(self, scored):
        curve = {round(p.threshold, 6): p for p in cg.sweep(scored)}
        pt = curve[0.5]
        # Injected = every pair scoring >= 0.5 (three of them).
        assert pt.n_injected == 3
        assert pt.precision_rel == pytest.approx(1.0)  # 3/3 have rel >= 1
        assert pt.precision_strict == pytest.approx(2 / 3)  # 2/3 have rel == 2
        assert pt.recall2 == pytest.approx(1.0)  # both rel-2 pairs injected
        assert pt.harmful_injected == 0

    def test_metrics_at_the_loosest_threshold(self, scored):
        curve = {round(p.threshold, 6): p for p in cg.sweep(scored)}
        pt = curve[0.1]
        assert pt.n_injected == 5
        assert pt.precision_rel == pytest.approx(0.6)  # 3/5
        assert pt.precision_strict == pytest.approx(0.4)  # 2/5
        assert pt.recall2 == pytest.approx(1.0)
        assert pt.harmful_injected == 2  # both rel-0 pairs injected

    def test_metrics_at_the_tightest_threshold(self, scored):
        curve = {round(p.threshold, 6): p for p in cg.sweep(scored)}
        pt = curve[0.9]
        assert pt.n_injected == 1
        assert pt.precision_rel == pytest.approx(1.0)
        assert pt.precision_strict == pytest.approx(1.0)
        assert pt.recall2 == pytest.approx(0.5)  # 1 of 2 rel-2 pairs
        assert pt.harmful_injected == 0


# ---------------------------------------------------------------------------
# choose
# ---------------------------------------------------------------------------


class TestChoose:
    def test_skips_higher_precision_point_that_misses_min_recall2(self):
        greedy = _point(0.9, precision_rel=1.0, recall2=0.4, n_injected=3)
        viable = _point(0.5, precision_rel=0.8, recall2=0.8, n_injected=12)
        loose = _point(0.2, precision_rel=0.6, recall2=1.0, n_injected=30)
        chosen = cg.choose([greedy, viable, loose], min_recall2=0.7)
        assert chosen is not None
        assert chosen.threshold == pytest.approx(0.5)

    def test_ties_prefer_the_higher_threshold(self):
        high = _point(0.7, precision_rel=0.8, recall2=0.9, n_injected=10)
        low = _point(0.4, precision_rel=0.8, recall2=1.0, n_injected=18)
        chosen = cg.choose([low, high], min_recall2=0.7)
        assert chosen is not None
        assert chosen.threshold == pytest.approx(0.7)

    def test_returns_none_when_no_point_meets_min_recall2(self):
        curve = [
            _point(0.9, precision_rel=1.0, recall2=0.3),
            _point(0.8, precision_rel=0.9, recall2=0.5),
        ]
        assert cg.choose(curve, min_recall2=0.7) is None

    def test_returns_none_on_empty_curve(self):
        assert cg.choose([], min_recall2=0.7) is None

    def test_min_recall2_boundary_is_inclusive(self):
        exact = _point(0.6, precision_rel=0.75, recall2=0.7)
        chosen = cg.choose([exact], min_recall2=0.7)
        assert chosen is not None
        assert chosen.threshold == pytest.approx(0.6)

    def test_default_min_recall2_is_0_7(self):
        below = _point(0.9, precision_rel=1.0, recall2=0.69)
        above = _point(0.4, precision_rel=0.5, recall2=0.71)
        chosen = cg.choose([below, above])
        assert chosen is not None
        assert chosen.threshold == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# render_markdown
# ---------------------------------------------------------------------------


class TestRenderMarkdown:
    def test_reports_chosen_threshold_model_date_and_sets(self):
        curve = [
            _point(0.5, precision_rel=0.8, recall2=0.8, n_injected=12),
            _point(0.3, precision_rel=0.6, recall2=1.0, n_injected=25),
        ]
        md = cg.render_markdown(
            curve,
            curve[0],
            model=DEFAULT_MODEL,
            n_pairs=52,
            sets={"util24": 29, "graded16": 23},
            date="2026-09-04",
        )
        # The threshold is model-specific and useless without the model name.
        assert DEFAULT_MODEL in md
        assert "0.5" in md
        assert "2026-09-04" in md
        assert "util24" in md and "graded16" in md
        assert "52" in md
        # Every curve row is reported, not just the winner.
        assert "0.3" in md

    def test_handles_no_viable_threshold(self):
        curve = [_point(0.9, precision_rel=1.0, recall2=0.3)]
        md = cg.render_markdown(
            curve,
            None,
            model=DEFAULT_MODEL,
            n_pairs=10,
            sets={"synthetic": 10},
            date="2026-09-04",
        )
        assert isinstance(md, str) and md.strip()
        assert DEFAULT_MODEL in md


# ---------------------------------------------------------------------------
# write_results
# ---------------------------------------------------------------------------


class TestWriteResults:
    def test_replaces_only_the_text_between_markers(self, tmp_path):
        p = tmp_path / "RESULTS.md"
        p.write_text(
            f"# Eval results\n\nkeep-this-preamble\n\n{START}\nSTALE CONTENT\n{END}\n\nkeep-this-tail\n",
            encoding="utf-8",
        )
        cg.write_results("## Rerank gate\n\nfresh body\n", p)

        text = p.read_text(encoding="utf-8")
        assert "STALE CONTENT" not in text
        assert "fresh body" in text
        assert "keep-this-preamble" in text
        assert "keep-this-tail" in text
        assert text.count(START) == 1
        assert text.count(END) == 1
        assert text.index(START) < text.index("fresh body") < text.index(END)

    def test_second_write_is_byte_identical(self, tmp_path):
        p = tmp_path / "RESULTS.md"
        p.write_text(
            f"# Eval results\n\nintro\n\n{START}\nold\n{END}\n\ntail\n",
            encoding="utf-8",
        )
        md = "## Rerank gate\n\nfresh body\n"
        cg.write_results(md, p)
        first = p.read_bytes()
        cg.write_results(md, p)
        assert p.read_bytes() == first

    def test_appends_a_marker_block_when_markers_are_absent(self, tmp_path):
        p = tmp_path / "RESULTS.md"
        p.write_text("# Eval results\n\nexisting content\n", encoding="utf-8")
        cg.write_results("## Rerank gate\n\nfresh body\n", p)

        text = p.read_text(encoding="utf-8")
        assert "existing content" in text
        assert text.count(START) == 1
        assert text.count(END) == 1
        assert text.index("existing content") < text.index(START)
        assert text.index(START) < text.index("fresh body") < text.index(END)

    def test_append_path_is_also_idempotent(self, tmp_path):
        p = tmp_path / "RESULTS.md"
        p.write_text("# Eval results\n\nexisting content\n", encoding="utf-8")
        md = "## Rerank gate\n\nfresh body\n"
        cg.write_results(md, p)
        first = p.read_bytes()
        cg.write_results(md, p)
        assert p.read_bytes() == first


# ---------------------------------------------------------------------------
# make_scorer (heavy)
# ---------------------------------------------------------------------------


@pytest.mark.embeddings
def test_make_scorer_real_model_smoke():
    """The scorer really is a cross-encoder over (prompt, text) pairs.

    Downloads ~0.15 GB into the fastembed cache on first run, so it lives
    behind the `embeddings` marker and never runs in `make test-ci`.
    """
    scorer = cg.make_scorer(DEFAULT_MODEL)
    scores = scorer(
        "production is on fire at 2am, what now",
        [
            "incident-runbook paste the rollback SQL inline first then page the on-call",
            "team-roster alphabetical list of engineers and their teams",
            "release-notes how releases are tagged in the changelog",
        ],
    )
    assert len(scores) == 3
    assert all(isinstance(s, float) for s in scores)
