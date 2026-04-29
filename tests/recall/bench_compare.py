"""Compare bench_e2e.py output against a checked-in baseline.

Used by CI as a quality gate: a PR fails if it regresses bucket-recall@5
on paraphrase queries by more than `--tolerance` percentage points.

Usage:

    python tests/recall/bench_compare.py \\
        --current ci-bench.json \\
        --baseline tests/recall/bench_baseline.json \\
        --tolerance 5

Exits 0 if PR is within tolerance (or improves), 1 if it regresses.
Prints a summary table comparing the two runs.

Why focus on bucket-paraphrase recall? It's the metric that captures the
real-use case: user types a paraphrased question, can the retriever surface
ANY relevant lesson in top-K. Lexical recall is at ceiling for everyone, so
regressions there are noise. Latency regressions are reported but don't fail
the gate (hardware-dependent).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load_results(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if "results" not in raw or not isinstance(raw["results"], list):
        raise SystemExit(f"{path}: invalid bench output (missing 'results' list)")
    return raw


def by_name(results: list[dict]) -> dict[str, dict]:
    return {r["name"]: r for r in results}


def fmt_pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def fmt_delta(delta: float) -> str:
    pp = delta * 100
    sign = "+" if pp >= 0 else ""
    return f"{sign}{pp:.0f}pp"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument(
        "--tolerance",
        type=float,
        default=5.0,
        help="Allowed drop in bucket-paraphrase recall@5 (percentage points). Default 5.",
    )
    parser.add_argument(
        "--gate-strategy",
        type=str,
        default="With recall (hybrid, warm)",
        help="Which strategy's bucket-paraphrase recall to gate on. Default: hybrid warm.",
    )
    args = parser.parse_args()

    cur = load_results(args.current)
    base = load_results(args.baseline)

    if cur["scale"] != base["scale"]:
        print(
            f"WARNING: scale mismatch — current={cur['scale']}, baseline={base['scale']}. "
            f"Comparison still proceeds but numbers may not be directly comparable.",
            file=sys.stderr,
        )

    cur_by = by_name(cur["results"])
    base_by = by_name(base["results"])

    common = sorted(set(cur_by) & set(base_by))
    if not common:
        print("ERROR: no overlapping strategy names between current and baseline", file=sys.stderr)
        return 1

    print(f"## Bench comparison (scale={cur['scale']}, n_eval={cur['n_eval_cases']})")
    print()
    print("| Strategy | Bucket recall@5 paraphrase (cur) | (base) | Δ | p50 ms (cur / base) |")
    print("|---|---|---|---|---|")
    regressions: list[str] = []
    for name in common:
        c = cur_by[name]
        b = base_by[name]
        cur_para = c["bucket_paraphrase"]
        base_para = b["bucket_paraphrase"]
        delta = cur_para - base_para
        gated = name == args.gate_strategy
        # Marker if regression beyond tolerance for the gated strategy
        marker = ""
        if gated and delta * 100 < -args.tolerance:
            marker = " ⛔"
            regressions.append(name)
        if gated:
            marker = "⭐" + marker if not regressions or regressions[-1] != name else marker
        print(
            f"| {name}{marker} | {fmt_pct(cur_para)} | {fmt_pct(base_para)} | "
            f"{fmt_delta(delta)} | {c['p50_ms']:.1f} / {b['p50_ms']:.1f} |"
        )
    print()
    print(f"⭐ = gated strategy ({args.gate_strategy}). ⛔ = regression beyond tolerance.")
    print()

    if regressions:
        print(
            f"FAIL: {len(regressions)} strategy regressed beyond -{args.tolerance}pp on bucket-paraphrase recall:",
            file=sys.stderr,
        )
        for r in regressions:
            c = cur_by[r]
            b = base_by[r]
            print(
                f"  - {r}: {fmt_pct(b['bucket_paraphrase'])} → {fmt_pct(c['bucket_paraphrase'])} "
                f"(Δ {fmt_delta(c['bucket_paraphrase'] - b['bucket_paraphrase'])})",
                file=sys.stderr,
            )
        return 1

    print(f"OK: gated strategy '{args.gate_strategy}' is within tolerance (-{args.tolerance}pp).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
