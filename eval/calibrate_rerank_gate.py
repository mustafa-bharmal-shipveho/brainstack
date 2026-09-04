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

The threshold is MODEL-SPECIFIC: cross-encoder outputs are raw model scores,
not calibrated probabilities. Re-run this whenever `ranking.reranker_model`
changes, and record the model name alongside the number.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

START_MARKER = "<!-- rerank-gate:start -->"
END_MARKER = "<!-- rerank-gate:end -->"

DEFAULT_MODEL = "jinaai/jina-reranker-v1-turbo-en"

# P4 acceptance bar: precision (relevance >= 1) on the injected set.
P4_MIN_PRECISION = 0.65

_REQUIRED_KEYS = ("prompt", "path", "relevance", "set")
_VALID_RELEVANCE = (0, 1, 2)


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
    """
    pairs: list[LabeledPair] = []
    text = Path(path).read_text(encoding="utf-8")
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno}: malformed JSON ({exc})") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{path}:{lineno}: expected a JSON object, got {type(row).__name__}")
        for key in _REQUIRED_KEYS:
            if key not in row:
                raise ValueError(f"{path}:{lineno}: missing required key {key!r}")
        relevance = row["relevance"]
        # `bool` is an `int` subclass and `1.5`/`"2"` are common label-file
        # slips; reject anything that is not literally 0, 1 or 2.
        if isinstance(relevance, bool) or not isinstance(relevance, int) or relevance not in _VALID_RELEVANCE:
            raise ValueError(
                f"{path}:{lineno}: relevance must be one of {_VALID_RELEVANCE}, got {relevance!r}"
            )
        pairs.append(
            LabeledPair(
                prompt=str(row["prompt"]),
                path=str(row["path"]),
                relevance=relevance,
                set_name=str(row["set"]),
            )
        )
    return pairs


def indexed_text_for_file(path: Path) -> str:
    """The SAME text `recall.sources.discover_documents` indexes for
    `path`: `_build_indexed_text(name, description, body)`, where `name`
    falls back to the file stem and `description` falls back to `""`.
    Scoring anything else calibrates a threshold for text the reranker
    will never actually see.
    """
    from recall.frontmatter import parse_path
    from recall.sources import _build_indexed_text

    p = Path(path)
    parsed = parse_path(p)
    fm = parsed.frontmatter or {}
    name = str(fm.get("name") or "") or p.stem
    description = str(fm.get("description") or "")
    return _build_indexed_text(name, description, parsed.body)


def make_scorer(model: str) -> "Callable[[str, list[str]], list[float]]":
    """Build a `(prompt, texts) -> scores` cross-encoder scorer for
    `model`. Heavy (downloads/loads a FastEmbed cross-encoder) — callers
    outside the `embeddings`-marked tests should treat this as expensive.
    """
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    from recall.qdrant_backend import _fastembed_cache_dir

    # Same weights directory the daemon uses, so calibrating does not
    # re-download 0.15 GB into $TMPDIR.
    encoder = TextCrossEncoder(model_name=model, cache_dir=_fastembed_cache_dir())

    def _score(prompt: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        return [float(s) for s in encoder.rerank(prompt, list(texts))]

    return _score


def score_pairs(
    pairs: Sequence[LabeledPair],
    scorer: "Callable[[str, list[str]], list[float]]",
    *,
    text_cap: int = 2000,
) -> list[ScoredPair]:
    """Score every pair with `scorer`, capping `indexed_text_for_file(...)`
    at `text_cap` chars (mirrors `recall.qdrant_backend.RERANK_TEXT_CAP`).
    One `ScoredPair` per input, in order.

    Pairs sharing a prompt are batched into one scorer call, which is how
    the daemon calls the encoder too (one query, N candidate texts).
    """
    by_prompt: dict[str, list[int]] = {}
    for i, pair in enumerate(pairs):
        by_prompt.setdefault(pair.prompt, []).append(i)

    scores: list[float] = [0.0] * len(pairs)
    for prompt, idxs in by_prompt.items():
        texts = [indexed_text_for_file(Path(pairs[i].path))[:text_cap] for i in idxs]
        got = list(scorer(prompt, texts))
        if len(got) != len(idxs):
            raise ValueError(
                f"scorer returned {len(got)} scores for {len(idxs)} texts (prompt {prompt!r})"
            )
        for i, s in zip(idxs, got):
            scores[i] = float(s)

    return [ScoredPair(pair=p, score=scores[i]) for i, p in enumerate(pairs)]


def sweep(scored: Sequence[ScoredPair]) -> list[CurvePoint]:
    """One `CurvePoint` per distinct score in `scored`, at threshold =
    that score: `n_injected` = pairs scoring >= threshold; `precision_rel`
    = fraction with relevance >= 1; `precision_strict` = fraction with
    relevance == 2; `recall2` = fraction of ALL relevance-2 pairs that are
    injected; `harmful_injected` = count with relevance == 0 injected.
    """
    if not scored:
        return []
    total_rel2 = sum(1 for s in scored if s.pair.relevance == 2)
    curve: list[CurvePoint] = []
    for threshold in sorted({s.score for s in scored}):
        injected = [s for s in scored if s.score >= threshold]
        n = len(injected)
        n_rel = sum(1 for s in injected if s.pair.relevance >= 1)
        n_strict = sum(1 for s in injected if s.pair.relevance == 2)
        harmful = sum(1 for s in injected if s.pair.relevance == 0)
        curve.append(
            CurvePoint(
                threshold=float(threshold),
                n_injected=n,
                precision_rel=(n_rel / n) if n else 0.0,
                precision_strict=(n_strict / n) if n else 0.0,
                # No relevance-2 pairs at all: recall is vacuously satisfied
                # rather than 0, otherwise `choose` could never pick anything.
                recall2=(n_strict / total_rel2) if total_rel2 else 1.0,
                harmful_injected=harmful,
            )
        )
    return curve


def choose(curve: Sequence[CurvePoint], *, min_recall2: float = 0.7) -> "CurvePoint | None":
    """The point maximizing precision subject to `recall2 >= min_recall2`;
    ties prefer the higher threshold. `None` when no point meets the
    constraint or the curve is empty.
    """
    viable = [p for p in curve if p.recall2 >= min_recall2]
    if not viable:
        return None
    return max(viable, key=lambda p: (p.precision_rel, p.precision_strict, p.threshold))


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
    """
    set_summary = ", ".join(f"`{name}` {count}" for name, count in sorted(sets.items())) or "none"
    lines: list[str] = [
        "## Rerank relevance gate (S4)",
        "",
        f"Model: `{model}` · pairs: {n_pairs} ({set_summary}) · date: {date}",
        "",
    ]

    if chosen is None:
        lines += [
            f"**No viable threshold**: no point on the curve reaches the required "
            f"relevance-2 recall. Leave `auto_recall_min_rerank` unset (`None`, gate off) "
            f"until the labelled set grows or the model changes.",
            "",
        ]
    else:
        # P4 acceptance: precision >= 0.65 on the injected set AND zero
        # harmful (relevance-0) injections at the chosen threshold.
        precision_ok = chosen.precision_rel >= P4_MIN_PRECISION
        harmful_ok = chosen.harmful_injected == 0
        verdict = "HOLDS" if (precision_ok and harmful_ok) else "DOES NOT HOLD"
        reasons = []
        if not precision_ok:
            reasons.append(
                f"precision {chosen.precision_rel:.3f} < {P4_MIN_PRECISION:.2f}"
            )
        if not harmful_ok:
            reasons.append(f"{chosen.harmful_injected} harmful (rel==0) pairs injected")
        detail = f" ({'; '.join(reasons)})" if reasons else ""
        lines += [
            f"**Chosen threshold: `auto_recall_min_rerank = {chosen.threshold:.4f}`** "
            f"— {chosen.n_injected} pairs injected, precision (rel>=1) "
            f"{chosen.precision_rel:.3f}, strict precision (rel==2) "
            f"{chosen.precision_strict:.3f}, relevance-2 recall {chosen.recall2:.3f}, "
            f"harmful (rel==0) injected {chosen.harmful_injected}.",
            "",
            f"**P4 acceptance (precision >= {P4_MIN_PRECISION:.2f} on the injected "
            f"set, harmful == 0): {verdict}**{detail}.",
            "",
        ]

    if curve:
        scores = [p.threshold for p in curve]
        lines += [
            f"Observed score range: {min(scores):.4f} – {max(scores):.4f} "
            "(raw cross-encoder outputs, not calibrated probabilities — the "
            "threshold does not transfer to another model).",
            "",
        ]

    lines += [
        "| threshold | injected | precision (rel>=1) | precision (rel==2) | recall (rel==2) | harmful | |",
        "|---:|---:|---:|---:|---:|---:|:--|",
    ]
    for point in sorted(curve, key=lambda p: -p.threshold):
        marker = "**chosen**" if chosen is not None and point is chosen else ""
        lines.append(
            f"| {point.threshold:.4f} | {point.n_injected} | {point.precision_rel:.3f} "
            f"| {point.precision_strict:.3f} | {point.recall2:.3f} "
            f"| {point.harmful_injected} | {marker} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_results(md: str, results_path: Path) -> None:
    """Replace the text between `<!-- rerank-gate:start -->` and
    `<!-- rerank-gate:end -->` in `results_path` with `md`, appending a
    fresh marker block at the end of the file when the markers are
    absent. Idempotent: writing the same `md` twice is byte-identical.
    """
    p = Path(results_path)
    original = p.read_text(encoding="utf-8") if p.exists() else ""
    block = f"{START_MARKER}\n{md.strip()}\n{END_MARKER}\n"

    start = original.find(START_MARKER)
    end = original.find(END_MARKER)
    if start != -1 and end != -1 and end > start:
        head = original[:start]
        tail = original[end + len(END_MARKER):]
        # The marker block owns its own trailing newline, so drop a leading
        # one from the tail to keep repeated writes byte-identical.
        if tail.startswith("\n"):
            tail = tail[1:]
        new_text = head + block + tail
    else:
        head = original
        if head and not head.endswith("\n"):
            head += "\n"
        if head:
            head += "\n"
        new_text = head + block

    p.write_text(new_text, encoding="utf-8")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--min-recall2", type=float, default=0.7)
    parser.add_argument("--write-results", action="store_true")
    parser.add_argument(
        "--curve-out", type=Path, default=Path("eval/rerank_gate_curve.json")
    )
    parser.add_argument("--scores-cache", type=Path, default=None)
    return parser


def _load_scores_cache(path: "Path | None", model: str) -> "dict[str, float] | None":
    if path is None or not Path(path).exists():
        return None
    try:
        blob = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(blob, dict) or blob.get("model") != model:
        return None
    scores = blob.get("scores")
    return scores if isinstance(scores, dict) else None


def _cache_key(pair: LabeledPair) -> str:
    return f"{pair.prompt}\x00{pair.path}"


def main(argv: "list[str] | None" = None) -> int:
    """CLI entry point: load labels, score, sweep, choose, report."""
    args = _build_arg_parser().parse_args(argv)

    try:
        pairs = load_labels(args.labels)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}")
        return 2
    if not pairs:
        print(f"error: no labelled pairs in {args.labels}")
        return 2

    missing = [p.path for p in pairs if not Path(p.path).exists()]
    if missing:
        print(f"warning: {len(missing)} labelled path(s) do not exist and will score on empty text:")
        for path in missing[:10]:
            print(f"  - {path}")

    sets: dict[str, int] = {}
    for pair in pairs:
        sets[pair.set_name] = sets.get(pair.set_name, 0) + 1

    cached = _load_scores_cache(args.scores_cache, args.model)
    if cached is not None and all(_cache_key(p) in cached for p in pairs):
        print(f"using cached scores from {args.scores_cache}")
        scored = [ScoredPair(pair=p, score=float(cached[_cache_key(p)])) for p in pairs]
        elapsed = 0.0
    else:
        print(f"loading cross-encoder {args.model} ...")
        t_load = time.perf_counter()
        scorer = make_scorer(args.model)
        print(f"loaded in {(time.perf_counter() - t_load) * 1000:.0f} ms")
        t0 = time.perf_counter()
        scored = score_pairs(pairs, scorer)
        elapsed = time.perf_counter() - t0
        print(f"scored {len(scored)} pairs in {elapsed * 1000:.0f} ms")
        if args.scores_cache is not None:
            Path(args.scores_cache).parent.mkdir(parents=True, exist_ok=True)
            Path(args.scores_cache).write_text(
                json.dumps(
                    {
                        "model": args.model,
                        "scores": {_cache_key(s.pair): s.score for s in scored},
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

    curve = sweep(scored)
    chosen = choose(curve, min_recall2=args.min_recall2)
    date = time.strftime("%Y-%m-%d")
    md = render_markdown(
        curve,
        chosen,
        model=args.model,
        n_pairs=len(pairs),
        sets=sets,
        date=date,
    )
    print()
    print(md)

    if args.curve_out is not None:
        Path(args.curve_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.curve_out).write_text(
            json.dumps(
                {
                    "model": args.model,
                    "date": date,
                    "n_pairs": len(pairs),
                    "sets": sets,
                    "min_recall2": args.min_recall2,
                    "scoring_seconds": round(elapsed, 4),
                    "chosen": None if chosen is None else vars(chosen),
                    "curve": [vars(p) for p in curve],
                    # Deliberately NO prompts and NO paths: labelled prompts
                    # are the user's real questions and the paths point into
                    # their brain. This file is meant to be committable, so it
                    # carries only (score, relevance, set) — enough to re-plot
                    # or re-sweep, identifying nothing. Use `--scores-cache`
                    # (never committed) when you need the raw pairs back.
                    "score_distribution": sorted(
                        (
                            {
                                "score": s.score,
                                "relevance": s.pair.relevance,
                                "set": s.pair.set_name,
                            }
                            for s in scored
                        ),
                        key=lambda d: -d["score"],
                    ),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"curve written to {args.curve_out}")

    if args.write_results:
        results_path = Path(__file__).resolve().parent / "RESULTS.md"
        write_results(md, results_path)
        print(f"results written to {results_path}")

    return 0 if chosen is not None else 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
