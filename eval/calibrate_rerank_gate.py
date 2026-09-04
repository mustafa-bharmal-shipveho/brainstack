"""S4 relevance-gate threshold calibration.

Turns a labelled `(prompt, path, relevance)` set into a threshold for
`auto_recall_min_rerank`. Heavy (real cross-encoder scoring) parts are
marked `@pytest.mark.embeddings` in the test suite and never run in `make
test-ci`; everything else here — label loading, the indexed-text mirror,
the sweep, the choice rule, the markdown render, and the marker-delimited
write into `eval/RESULTS.md` — is pure and hermetic.

Usage (manual, never in CI):

    .venv/bin/python3 eval/calibrate_rerank_gate.py \\
        --labels eval/labels/rerank_gate_labels.jsonl \\
        --model jinaai/jina-reranker-v1-turbo-en \\
        --min-recall2 0.7 --write-results

Scaffold: every dataclass is real (they are pure structure); every
function body raises `NotImplementedError("scaffold")` pending the
Development phase. `main` returns 2 unconditionally. See
tests/eval/test_calibrate_rerank_gate.py.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


@dataclass
class LabeledPair:
    """One labelled `(prompt, path)` judgment.

    `relevance` is 0 (irrelevant), 1 (tangential), or 2 (directly
    relevant). `set_name` identifies which labelled set the pair came from
    (e.g. "util24", "graded16") so `render_markdown` can report per-set
    counts.
    """

    prompt: str
    path: str
    relevance: int
    set_name: str


@dataclass
class ScoredPair:
    """A `LabeledPair` plus the cross-encoder score assigned to it."""

    pair: LabeledPair
    score: float


@dataclass
class CurvePoint:
    """Precision/recall at one candidate threshold, swept over every
    distinct score observed in the scored set."""

    threshold: float
    n_injected: int
    precision_rel: float
    precision_strict: float
    recall2: float
    harmful_injected: int


def load_labels(path: Path) -> list[LabeledPair]:
    """Parse a `{"prompt","path","relevance","set",...}` JSONL file into
    `LabeledPair`s. Blank lines are skipped. Extra keys (e.g. `"used"` in
    the real labelled file) are tolerated. Raises `ValueError` on
    malformed JSON, a missing required key, or `relevance` outside
    `{0, 1, 2}`.

    Scaffold: signature + docstring only. See
    tests/eval/test_calibrate_rerank_gate.py::TestLoadLabels.
    """
    raise NotImplementedError("scaffold")


def indexed_text_for_file(path: Path) -> str:
    """The SAME text `recall.sources.discover_documents` indexes for
    `path`: `_build_indexed_text(name, description, body)`, where `name`
    falls back to the file stem and `description` falls back to `""`.
    Scoring anything else calibrates a threshold for text the reranker
    will never actually see.

    Scaffold: signature + docstring only. See
    tests/eval/test_calibrate_rerank_gate.py::TestIndexedTextForFile.
    """
    raise NotImplementedError("scaffold")


def make_scorer(model: str) -> "Callable[[str, list[str]], list[float]]":
    """Build a `(prompt, texts) -> scores` cross-encoder scorer for
    `model`. Heavy (downloads/loads a FastEmbed cross-encoder) — callers
    outside the `embeddings`-marked tests should treat this as expensive.

    Scaffold: signature + docstring only. See
    tests/eval/test_calibrate_rerank_gate.py::test_make_scorer_real_model_smoke.
    """
    raise NotImplementedError("scaffold")


def score_pairs(
    pairs: Sequence[LabeledPair],
    scorer: "Callable[[str, list[str]], list[float]]",
    *,
    text_cap: int = 2000,
) -> list[ScoredPair]:
    """Score every pair with `scorer`, capping `indexed_text_for_file(...)`
    at `text_cap` chars (mirrors `recall.qdrant_backend.RERANK_TEXT_CAP`).
    One `ScoredPair` per input, in order.

    Scaffold: signature + docstring only. See
    tests/eval/test_calibrate_rerank_gate.py::TestScorePairs.
    """
    raise NotImplementedError("scaffold")


def sweep(scored: Sequence[ScoredPair]) -> list[CurvePoint]:
    """One `CurvePoint` per distinct score in `scored`, at threshold =
    that score: `n_injected` = pairs scoring >= threshold; `precision_rel`
    = fraction with relevance >= 1; `precision_strict` = fraction with
    relevance == 2; `recall2` = fraction of ALL relevance-2 pairs that are
    injected; `harmful_injected` = count with relevance == 0 injected.

    Scaffold: signature + docstring only. See
    tests/eval/test_calibrate_rerank_gate.py::TestSweep.
    """
    raise NotImplementedError("scaffold")


def choose(curve: Sequence[CurvePoint], *, min_recall2: float = 0.7) -> "CurvePoint | None":
    """The point maximizing precision subject to `recall2 >= min_recall2`;
    ties prefer the higher threshold. `None` when no point meets the
    constraint or the curve is empty.

    Scaffold: signature + docstring only. See
    tests/eval/test_calibrate_rerank_gate.py::TestChoose.
    """
    raise NotImplementedError("scaffold")


def render_markdown(
    curve: Sequence[CurvePoint],
    chosen: "CurvePoint | None",
    *,
    model: str,
    n_pairs: int,
    sets: dict[str, int],
    date: str,
) -> str:
    """Render the calibration report: chosen threshold (or "no viable
    threshold"), model name, date, per-set pair counts, and every curve
    row.

    Scaffold: signature + docstring only. See
    tests/eval/test_calibrate_rerank_gate.py::TestRenderMarkdown.
    """
    raise NotImplementedError("scaffold")


def write_results(md: str, results_path: Path) -> None:
    """Replace the text between `<!-- rerank-gate:start -->` and
    `<!-- rerank-gate:end -->` in `results_path` with `md`, appending a
    fresh marker block at the end of the file when the markers are
    absent. Idempotent: writing the same `md` twice is byte-identical.

    Scaffold: signature + docstring only. See
    tests/eval/test_calibrate_rerank_gate.py::TestWriteResults.
    """
    raise NotImplementedError("scaffold")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--model", default="jinaai/jina-reranker-v1-turbo-en")
    parser.add_argument("--min-recall2", type=float, default=0.7)
    parser.add_argument("--write-results", action="store_true")
    parser.add_argument(
        "--curve-out", type=Path, default=Path("eval/rerank_gate_curve.json")
    )
    parser.add_argument("--scores-cache", type=Path, default=None)
    return parser


def main(argv: "list[str] | None" = None) -> int:
    """CLI entry point. Scaffold: always returns 2 (unimplemented)."""
    return 2


if __name__ == "__main__":
    import sys

    sys.exit(main())
