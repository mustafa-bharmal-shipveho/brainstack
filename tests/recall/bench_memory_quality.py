"""Evaluate retrieval and budget quality against a real local brain.

This is an opt-in benchmark, not a pytest test. It reads a user's local
brainstack memory directory and reports aggregate quality signals:

  - whether target memories are reachable by a lexical retrieval baseline
  - how much token budget is needed before the target memory enters context
  - whether the runtime event log shows eviction churn or eviction regret

It intentionally has no Qdrant/FastEmbed dependency. The goal is a cheap,
private, local signal you can run before the heavier hybrid recall path exists
on the machine.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from recall.frontmatter import parse_path
from runtime.adapters.claude_code.config import RuntimeConfig
from runtime.core.events import load_events
from runtime.core.policy.defaults.lru import LRUPolicy
from runtime.core.replay import ReplayConfig, iter_engine_steps
from runtime.core.tokens import OfflineTokenCounter


STOPWORDS = frozenset(
    """
    a an and are as at be by can did do does for from had has have how i if in
    into is it its me my not of on or our should so than that the their them
    then there this to use was we what when where which who why with you your
    """.split()
)

WORD_RE = re.compile(r"[a-z0-9_]+")
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)

DEFAULT_BUDGETS = [500, 1000, 2000, 4000, 8000, 12000, 20000, 36000]
DEFAULT_RUNTIME_RETRIEVED_BUDGETS = [5000, 10000, 20000, 40000, 80000]


@dataclass(frozen=True)
class MemoryDoc:
    rel_path: str
    title: str
    description: str
    text: str
    token_count: int
    kind: str


@dataclass(frozen=True)
class EvalCase:
    query: str
    target_path: str
    kind: str


@dataclass
class RetrievalSummary:
    n_docs: int
    n_cases: int
    total_doc_tokens: int
    p50_doc_tokens: float
    p95_doc_tokens: float
    recall_at_1: float
    recall_at_3: float
    recall_at_5: float
    recall_at_10: float
    p50_target_depth_tokens: float
    p95_target_depth_tokens: float
    budget_hit_rates: dict[str, float]


@dataclass
class RuntimeSummary:
    event_log: str
    n_events: int
    n_steps: int
    n_items_added: int
    n_engine_evictions: int
    n_eviction_steps: int
    engine_evictions_by_bucket: dict[str, int]
    n_same_id_readds_after_eviction: int
    n_same_source_readds_after_eviction: int
    final_items: int
    final_budget_used: int
    final_budget_total: int
    final_tokens_by_bucket: dict[str, int]
    final_items_by_bucket: dict[str, int]


@dataclass
class RuntimeSweepRow:
    retrieved_budget: int
    total_budget: int
    n_engine_evictions: int
    n_same_id_readds_after_eviction: int
    n_same_source_readds_after_eviction: int
    final_items: int
    final_budget_used: int


@dataclass
class ScaleSimulationSummary:
    mode: str
    factor: int
    n_docs: int
    n_cases: int
    build_ms: float
    query_p50_ms: float
    query_p95_ms: float
    family_recall_at_1: float
    family_recall_at_5: float
    exact_recall_at_1: float
    exact_recall_at_5: float
    family_target_depth_p50: float
    family_target_depth_p95: float
    exact_target_depth_p50: float
    exact_target_depth_p95: float
    family_budget_hit_rates: dict[str, float]


def _memory_root(brain_root: Path) -> Path:
    return brain_root / "memory" if (brain_root / "memory").is_dir() else brain_root


def _skip_rel_path(rel: str, *, include_archived: bool) -> bool:
    if rel in {"MEMORY.md", "semantic/LESSONS.md"}:
        return True
    if rel.startswith("working/"):
        return True
    if not include_archived and rel.startswith("semantic/archived/"):
        return True
    return False


def _h1(body: str) -> str:
    match = H1_RE.search(body)
    return match.group(1).strip() if match else ""


def _humanize_slug(value: str) -> str:
    return re.sub(r"[_\\-]+", " ", value).strip()


def _terms(text: str) -> list[str]:
    return [
        tok
        for tok in WORD_RE.findall(text.lower())
        if len(tok) > 1 and tok not in STOPWORDS
    ]


def load_memory_docs(
    brain_root: Path,
    *,
    include_archived: bool = False,
) -> list[MemoryDoc]:
    root = _memory_root(brain_root).expanduser()
    counter = OfflineTokenCounter()
    docs: list[MemoryDoc] = []
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root).as_posix()
        if _skip_rel_path(rel, include_archived=include_archived):
            continue
        parsed = parse_path(path)
        body = parsed.body.strip()
        fm = parsed.frontmatter or {}
        name = str(fm.get("name") or path.stem)
        description = str(fm.get("description") or "")
        title = name or _h1(body) or path.stem
        kind = str(fm.get("type") or rel.split("/", 1)[0])
        indexed_text = f"{name} {description} {description} {description} {body}".strip()
        if not indexed_text:
            continue
        docs.append(
            MemoryDoc(
                rel_path=rel,
                title=title,
                description=description,
                text=indexed_text,
                token_count=counter.count(indexed_text),
                kind=kind,
            )
        )
    return docs


def build_eval_cases(docs: Iterable[MemoryDoc]) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for doc in docs:
        if doc.description and len(_terms(doc.description)) >= 3:
            cases.append(
                EvalCase(
                    query=doc.description,
                    target_path=doc.rel_path,
                    kind="description",
                )
            )
        else:
            fallback = _humanize_slug(doc.title)
            if len(_terms(fallback)) >= 2:
                cases.append(
                    EvalCase(
                        query=fallback,
                        target_path=doc.rel_path,
                        kind="title",
                    )
                )
    return cases


class BM25Index:
    def __init__(self, docs: list[MemoryDoc]) -> None:
        self.docs = docs
        self.doc_terms: dict[str, Counter[str]] = {}
        self.doc_lens: dict[str, int] = {}
        df: Counter[str] = Counter()
        for doc in docs:
            counts = Counter(_terms(doc.text))
            self.doc_terms[doc.rel_path] = counts
            self.doc_lens[doc.rel_path] = sum(counts.values())
            df.update(counts.keys())
        self.df = df
        self.avgdl = statistics.mean(self.doc_lens.values()) if self.doc_lens else 1.0

    def rank(self, query: str) -> list[MemoryDoc]:
        q_terms = Counter(_terms(query))
        n_docs = max(1, len(self.docs))
        scored: list[tuple[float, str, MemoryDoc]] = []
        for doc in self.docs:
            counts = self.doc_terms[doc.rel_path]
            doc_len = self.doc_lens[doc.rel_path] or 1
            score = 0.0
            for term, qtf in q_terms.items():
                tf = counts.get(term, 0)
                if tf <= 0:
                    continue
                idf = math.log(1 + (n_docs - self.df[term] + 0.5) / (self.df[term] + 0.5))
                denom = tf + 1.2 * (1 - 0.75 + 0.75 * doc_len / self.avgdl)
                score += qtf * idf * (tf * 2.2 / denom)
            scored.append((score, doc.rel_path, doc))
        scored.sort(key=lambda row: (-row[0], row[1]))
        return [doc for score, _rel, doc in scored if score > 0]


def summarize_retrieval(docs: list[MemoryDoc], cases: list[EvalCase], budgets: list[int]) -> RetrievalSummary:
    index = BM25Index(docs)
    doc_by_path = {doc.rel_path: doc for doc in docs}
    hit_counts = {1: 0, 3: 0, 5: 0, 10: 0}
    budget_hits = {b: 0 for b in budgets}
    depths: list[int] = []
    for case in cases:
        ranked = index.rank(case.query)
        ranked_paths = [doc.rel_path for doc in ranked]
        try:
            rank_idx = ranked_paths.index(case.target_path)
        except ValueError:
            depth = sum(doc.token_count for doc in docs) + doc_by_path[case.target_path].token_count
            depths.append(depth)
            continue

        for k in hit_counts:
            if rank_idx < k:
                hit_counts[k] += 1

        depth = sum(doc.token_count for doc in ranked[: rank_idx + 1])
        depths.append(depth)
        for budget in budgets:
            if depth <= budget:
                budget_hits[budget] += 1

    n_cases = max(1, len(cases))
    doc_tokens = sorted(doc.token_count for doc in docs)
    return RetrievalSummary(
        n_docs=len(docs),
        n_cases=len(cases),
        total_doc_tokens=sum(doc_tokens),
        p50_doc_tokens=_percentile(doc_tokens, 50),
        p95_doc_tokens=_percentile(doc_tokens, 95),
        recall_at_1=hit_counts[1] / n_cases,
        recall_at_3=hit_counts[3] / n_cases,
        recall_at_5=hit_counts[5] / n_cases,
        recall_at_10=hit_counts[10] / n_cases,
        p50_target_depth_tokens=_percentile(depths, 50),
        p95_target_depth_tokens=_percentile(depths, 95),
        budget_hit_rates={str(b): budget_hits[b] / n_cases for b in budgets},
    )


def simulate_scaled_memory(
    docs: list[MemoryDoc],
    cases: list[EvalCase],
    *,
    factor: int,
    mode: str,
    budgets: list[int],
) -> ScaleSimulationSummary:
    """Simulate a much larger memory using the current local memory as seed.

    The simulation keeps only title + weighted description in the ranking text
    so it can scale to tens of thousands of docs without writing a private
    corpus to disk. Token depths still use each seed doc's real token size.

    `family` means any synthetic variant derived from the expected memory.
    That is usually the right user-facing metric: if 1000 near-duplicates all
    contain the same lesson, retrieving any one of them answers the question.
    `exact` is also reported to show how hard exact-document retrieval becomes
    when the memory has many near-duplicates.
    """
    if factor <= 1:
        raise ValueError("factor must be greater than 1")
    if mode not in {"clean", "noisy"}:
        raise ValueError("mode must be 'clean' or 'noisy'")

    seed_docs = [doc for doc in docs if doc.description or len(_terms(doc.title)) >= 2]
    rng = __import__("random").Random(42)
    family_by_path: dict[str, str] = {}
    synthetic_docs: list[MemoryDoc] = []
    common_noise = (
        "agent memory context runtime recall quality session project tool code "
        "review data command output file"
    )

    for doc in seed_docs:
        description = doc.description or _humanize_slug(doc.title)
        for i in range(factor):
            rel_path = doc.rel_path if i == 0 else f"synthetic/{doc.rel_path[:-3]}__v{i:04d}.md"
            text = f"{doc.title} {description} {description} {description}"
            if mode == "noisy":
                other = seed_docs[rng.randrange(len(seed_docs))]
                other_description = other.description or _humanize_slug(other.title)
                borrowed = " ".join(_terms(other_description)[:6])
                text = f"{text} {common_noise} {borrowed}"
            synthetic_docs.append(
                MemoryDoc(
                    rel_path=rel_path,
                    title=doc.title,
                    description=description,
                    text=text,
                    token_count=doc.token_count,
                    kind=doc.kind,
                )
            )
            family_by_path[rel_path] = doc.rel_path

    import time

    started = time.perf_counter()
    index = BM25Index(synthetic_docs)
    build_ms = (time.perf_counter() - started) * 1000

    exact_hits = {1: 0, 5: 0}
    family_hits = {1: 0, 5: 0}
    exact_depths: list[int] = []
    family_depths: list[int] = []
    query_times: list[float] = []

    for case in cases:
        started = time.perf_counter()
        ranked = index.rank(case.query)
        query_times.append((time.perf_counter() - started) * 1000)
        paths = [doc.rel_path for doc in ranked]

        try:
            exact_idx = paths.index(case.target_path)
        except ValueError:
            exact_idx = None

        family_idx: int | None = None
        for idx, path in enumerate(paths):
            if family_by_path.get(path) == case.target_path:
                family_idx = idx
                break

        for k in exact_hits:
            if exact_idx is not None and exact_idx < k:
                exact_hits[k] += 1
            if family_idx is not None and family_idx < k:
                family_hits[k] += 1

        total_tokens = sum(doc.token_count for doc in seed_docs) * factor
        exact_depths.append(
            total_tokens
            if exact_idx is None
            else sum(doc.token_count for doc in ranked[: exact_idx + 1])
        )
        family_depths.append(
            total_tokens
            if family_idx is None
            else sum(doc.token_count for doc in ranked[: family_idx + 1])
        )

    n_cases = max(1, len(cases))
    family_budget_hits = {
        str(budget): sum(1 for depth in family_depths if depth <= budget) / n_cases
        for budget in budgets
    }
    return ScaleSimulationSummary(
        mode=mode,
        factor=factor,
        n_docs=len(synthetic_docs),
        n_cases=len(cases),
        build_ms=build_ms,
        query_p50_ms=_percentile([int(v) for v in query_times], 50),
        query_p95_ms=_percentile([int(v) for v in query_times], 95),
        family_recall_at_1=family_hits[1] / n_cases,
        family_recall_at_5=family_hits[5] / n_cases,
        exact_recall_at_1=exact_hits[1] / n_cases,
        exact_recall_at_5=exact_hits[5] / n_cases,
        family_target_depth_p50=_percentile(family_depths, 50),
        family_target_depth_p95=_percentile(family_depths, 95),
        exact_target_depth_p50=_percentile(exact_depths, 50),
        exact_target_depth_p95=_percentile(exact_depths, 95),
        family_budget_hit_rates=family_budget_hits,
    )


def summarize_runtime(event_log: Path, budgets: dict[str, int]) -> RuntimeSummary | None:
    event_log = event_log.expanduser()
    if not event_log.exists():
        return None

    events = load_events(event_log)
    config = ReplayConfig(
        budgets=dict(budgets),
        policy=LRUPolicy(),
        session_id="memory-quality-eval",
    )
    steps = list(iter_engine_steps(events, config))
    if not steps:
        return RuntimeSummary(
            event_log=str(event_log),
            n_events=len(events),
            n_steps=0,
            n_items_added=0,
            n_engine_evictions=0,
            n_eviction_steps=0,
            engine_evictions_by_bucket={},
            n_same_id_readds_after_eviction=0,
            n_same_source_readds_after_eviction=0,
            final_items=0,
            final_budget_used=0,
            final_budget_total=sum(budgets.values()),
            final_tokens_by_bucket={},
            final_items_by_bucket={},
        )

    id_to_source: dict[str, str] = {}
    id_to_bucket: dict[str, str] = {}
    evicted_ids_seen: set[str] = set()
    evicted_sources_seen: set[str] = set()
    evictions_by_bucket: Counter[str] = Counter()
    same_id_readds = 0
    same_source_readds = 0

    for step in steps:
        for item in step.event.items_added:
            item_id = getattr(item, "id", "")
            source_path = getattr(item, "source_path", "")
            bucket = getattr(item, "bucket", "")
            if item_id:
                if item_id in evicted_ids_seen:
                    same_id_readds += 1
                id_to_source[item_id] = source_path
                id_to_bucket[item_id] = bucket
            if source_path and not source_path.startswith("<tool:"):
                if source_path in evicted_sources_seen:
                    same_source_readds += 1

        for evicted_id in step.evicted_ids:
            evicted_ids_seen.add(evicted_id)
            evictions_by_bucket[id_to_bucket.get(evicted_id, "unknown")] += 1
            source_path = id_to_source.get(evicted_id, "")
            if source_path and not source_path.startswith("<tool:"):
                evicted_sources_seen.add(source_path)

    final = steps[-1].manifest
    final_items_by_bucket: Counter[str] = Counter()
    final_tokens_by_bucket: Counter[str] = Counter()
    for item in final.items:
        final_items_by_bucket[item.bucket] += 1
        final_tokens_by_bucket[item.bucket] += item.token_count

    return RuntimeSummary(
        event_log=str(event_log),
        n_events=len(events),
        n_steps=len(steps),
        n_items_added=sum(len(event.items_added) for event in events),
        n_engine_evictions=sum(len(step.evicted_ids) for step in steps),
        n_eviction_steps=sum(1 for step in steps if step.evicted_ids),
        engine_evictions_by_bucket=dict(sorted(evictions_by_bucket.items())),
        n_same_id_readds_after_eviction=same_id_readds,
        n_same_source_readds_after_eviction=same_source_readds,
        final_items=len(final.items),
        final_budget_used=final.budget_used,
        final_budget_total=final.budget_total,
        final_tokens_by_bucket=dict(sorted(final_tokens_by_bucket.items())),
        final_items_by_bucket=dict(sorted(final_items_by_bucket.items())),
    )


def summarize_runtime_sweep(
    event_log: Path,
    base_budgets: dict[str, int],
    retrieved_budgets: list[int],
) -> list[RuntimeSweepRow]:
    rows: list[RuntimeSweepRow] = []
    for retrieved_budget in retrieved_budgets:
        budgets = dict(base_budgets)
        budgets["retrieved"] = retrieved_budget
        summary = summarize_runtime(event_log, budgets)
        if summary is None:
            continue
        rows.append(
            RuntimeSweepRow(
                retrieved_budget=retrieved_budget,
                total_budget=summary.final_budget_total,
                n_engine_evictions=summary.n_engine_evictions,
                n_same_id_readds_after_eviction=summary.n_same_id_readds_after_eviction,
                n_same_source_readds_after_eviction=summary.n_same_source_readds_after_eviction,
                final_items=summary.final_items,
                final_budget_used=summary.final_budget_used,
            )
        )
    return rows


def _percentile(values: list[int], pct: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = int(round((len(ordered) - 1) * pct / 100))
    return float(ordered[idx])


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_markdown(
    retrieval: RetrievalSummary,
    runtime: RuntimeSummary | None,
    runtime_sweep: list[RuntimeSweepRow],
    scale_simulations: list[ScaleSimulationSummary],
) -> str:
    lines = [
        "# Memory Quality Evaluation",
        "",
        "## Retrieval Baseline",
        "",
        f"- Active memory docs: {retrieval.n_docs}",
        f"- Generated eval cases: {retrieval.n_cases}",
        f"- Total indexed tokens: {retrieval.total_doc_tokens:,}",
        f"- Doc size: p50={retrieval.p50_doc_tokens:.0f} tokens, p95={retrieval.p95_doc_tokens:.0f} tokens",
        "",
        "| Metric | Result |",
        "|---|---:|",
        f"| Lexical recall@1 | {_pct(retrieval.recall_at_1)} |",
        f"| Lexical recall@3 | {_pct(retrieval.recall_at_3)} |",
        f"| Lexical recall@5 | {_pct(retrieval.recall_at_5)} |",
        f"| Lexical recall@10 | {_pct(retrieval.recall_at_10)} |",
        f"| Target depth p50 | {retrieval.p50_target_depth_tokens:.0f} tokens |",
        f"| Target depth p95 | {retrieval.p95_target_depth_tokens:.0f} tokens |",
        "",
        "Budget hit rate means: if ranked memories are packed into a context budget,",
        "how often does the expected memory fit before the budget runs out?",
        "",
        "| Context budget | Expected memory included |",
        "|---:|---:|",
    ]
    for budget, rate in retrieval.budget_hit_rates.items():
        lines.append(f"| {int(budget):,} tokens | {_pct(rate)} |")

    lines.extend(["", "## Runtime Event Log", ""])
    if runtime is None:
        lines.append("- No runtime event log found.")
    else:
        lines.extend(
            [
                f"- Events: {runtime.n_events}",
                f"- Items added: {runtime.n_items_added}",
                f"- Engine evictions under current budgets: {runtime.n_engine_evictions}",
                f"- Steps with evictions: {runtime.n_eviction_steps}",
                f"- Evictions by bucket: {runtime.engine_evictions_by_bucket}",
                f"- Same item re-added after eviction: {runtime.n_same_id_readds_after_eviction}",
                f"- Same source re-read after eviction: {runtime.n_same_source_readds_after_eviction}",
                f"- Final manifest: {runtime.final_items} items, {runtime.final_budget_used:,} / {runtime.final_budget_total:,} tokens",
                "",
                "| Bucket | Items | Tokens |",
                "|---|---:|---:|",
            ]
        )
        buckets = sorted(set(runtime.final_items_by_bucket) | set(runtime.final_tokens_by_bucket))
        for bucket in buckets:
            lines.append(
                f"| {bucket} | {runtime.final_items_by_bucket.get(bucket, 0)} | "
                f"{runtime.final_tokens_by_bucket.get(bucket, 0):,} |"
            )

        if runtime_sweep:
            lines.extend(
                [
                    "",
                    "### Retrieved-Budget Sweep",
                    "",
                    "Same event log, same LRU policy, only the `retrieved` bucket cap changes.",
                    "",
                    "| retrieved cap | total cap | evictions | same-id re-adds | same-source re-reads | final items | final used |",
                    "|---:|---:|---:|---:|---:|---:|---:|",
                ]
            )
            for row in runtime_sweep:
                lines.append(
                    f"| {row.retrieved_budget:,} | {row.total_budget:,} | "
                    f"{row.n_engine_evictions} | {row.n_same_id_readds_after_eviction} | "
                    f"{row.n_same_source_readds_after_eviction} | {row.final_items} | "
                    f"{row.final_budget_used:,} |"
                )

    if scale_simulations:
        lines.extend(
            [
                "",
                "## Scaled Memory Simulation",
                "",
                "Family recall means any synthetic variant of the expected memory was retrieved.",
                "Exact recall means the original seed file itself was retrieved.",
                "",
                "| Mode | Factor | Docs | family@1 | family@5 | exact@1 | exact@5 | family p95 depth | exact p95 depth | p50 query ms |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for sim in scale_simulations:
            lines.append(
                f"| {sim.mode} | {sim.factor}x | {sim.n_docs:,} | "
                f"{_pct(sim.family_recall_at_1)} | {_pct(sim.family_recall_at_5)} | "
                f"{_pct(sim.exact_recall_at_1)} | {_pct(sim.exact_recall_at_5)} | "
                f"{sim.family_target_depth_p95:.0f} tok | {sim.exact_target_depth_p95:.0f} tok | "
                f"{sim.query_p50_ms:.0f} |"
            )
        lines.extend(["", "| Context budget | Clean family hit | Noisy family hit |", "|---:|---:|---:|"])
        by_mode = {sim.mode: sim for sim in scale_simulations}
        budget_keys = sorted(
            {
                int(k)
                for sim in scale_simulations
                for k in sim.family_budget_hit_rates.keys()
            }
        )
        for budget in budget_keys:
            clean = by_mode.get("clean")
            noisy = by_mode.get("noisy")
            lines.append(
                f"| {budget:,} tokens | "
                f"{_pct(clean.family_budget_hit_rates.get(str(budget), 0.0)) if clean else '-'} | "
                f"{_pct(noisy.family_budget_hit_rates.get(str(budget), 0.0)) if noisy else '-'} |"
            )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--brain-root",
        type=Path,
        default=Path("~/.agent").expanduser(),
        help="Brain root or memory root. Default: ~/.agent",
    )
    parser.add_argument(
        "--event-log",
        type=Path,
        default=Path("~/.agent/runtime/logs/events.log.jsonl").expanduser(),
        help="Runtime event log. Default: ~/.agent/runtime/logs/events.log.jsonl",
    )
    parser.add_argument(
        "--budgets",
        default=",".join(str(v) for v in DEFAULT_BUDGETS),
        help="Comma-separated context budgets to evaluate.",
    )
    parser.add_argument(
        "--include-archived",
        action="store_true",
        help="Include semantic/archived markdown memories.",
    )
    parser.add_argument(
        "--runtime-retrieved-budgets",
        default=",".join(str(v) for v in DEFAULT_RUNTIME_RETRIEVED_BUDGETS),
        help="Comma-separated retrieved-bucket caps for event-log sensitivity analysis.",
    )
    parser.add_argument(
        "--simulate-scale",
        type=int,
        default=0,
        help="Generate an in-memory scaled corpus from local memory, e.g. 1000 for 1000x.",
    )
    parser.add_argument(
        "--simulate-target-docs",
        type=int,
        default=0,
        help="Generate an in-memory corpus near this total doc count, e.g. 5000.",
    )
    parser.add_argument(
        "--simulation-mode",
        choices=("clean", "noisy", "both"),
        default="both",
        help="Synthetic scale mode. Default: both.",
    )
    parser.add_argument("--json", type=Path, help="Optional JSON output path.")
    args = parser.parse_args()

    budgets = [int(part) for part in args.budgets.split(",") if part.strip()]
    runtime_retrieved_budgets = [
        int(part) for part in args.runtime_retrieved_budgets.split(",") if part.strip()
    ]
    docs = load_memory_docs(args.brain_root, include_archived=args.include_archived)
    cases = build_eval_cases(docs)
    retrieval = summarize_retrieval(docs, cases, budgets)

    runtime_cfg = RuntimeConfig.load()
    runtime = summarize_runtime(args.event_log, runtime_cfg.budgets)
    runtime_sweep = summarize_runtime_sweep(
        args.event_log,
        runtime_cfg.budgets,
        runtime_retrieved_budgets,
    )
    scale_simulations: list[ScaleSimulationSummary] = []
    simulate_factor = args.simulate_scale
    if args.simulate_target_docs:
        simulate_factor = max(2, round(args.simulate_target_docs / max(1, len(docs))))
    if simulate_factor:
        modes = ["clean", "noisy"] if args.simulation_mode == "both" else [args.simulation_mode]
        for mode in modes:
            scale_simulations.append(
                simulate_scaled_memory(
                    docs,
                    cases,
                    factor=simulate_factor,
                    mode=mode,
                    budgets=budgets,
                )
            )

    payload = {
        "retrieval": asdict(retrieval),
        "runtime": asdict(runtime) if runtime is not None else None,
        "runtime_sweep": [asdict(row) for row in runtime_sweep],
        "scale_simulations": [asdict(row) for row in scale_simulations],
    }
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(render_markdown(retrieval, runtime, runtime_sweep, scale_simulations))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
