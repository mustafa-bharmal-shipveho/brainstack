"""End-to-end retrieval benchmark: with-recall vs without-recall.

Generates a deterministic synthetic brain (80 lessons across 8 conceptual buckets)
+ 20 eval queries (10 lexical, 10 paraphrase). Times and scores four retrieval
strategies on the same eval set:

  1. without-recall: index-only          (substring match on MEMORY.md)
  2. without-recall: index + reads        (substring match, then read top-N bodies)
  3. with-recall:    BM25-only            (HybridRetriever, embedding_weight=0)
  4. with-recall:    hybrid (BM25+MiniLM) (HybridRetriever, both enabled)

Run:

    python tests/recall/bench_e2e.py --report

Reports a markdown table (recall@5 + p50/p95 latency, ms) to stdout. Not part of
the pytest suite — would slow it down. The synthetic generator is seeded so two
runs produce the same numbers given the same install.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import statistics
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


# ---------------------------------------------------------------------------
# Synthetic brain generator
# ---------------------------------------------------------------------------

# 8 conceptual buckets × 10 lessons = 80 lessons. Bucket names are intentionally
# generic / open-source-friendly — no Veho or other employer-specific content.
BUCKETS: list[dict] = [
    {
        "slug": "agent-team-workflow",
        "type": "feedback",
        "topic_words": ["agent team", "workflow", "phase", "wave", "TDD", "scaffold", "verify red"],
        "description_template": "{} cycle: {} before {}",
        "phrasings": [
            ("agent team", "scaffold tests", "writing implementation"),
            ("development", "lock the test suite", "starting feature work"),
            ("delivery", "run baseline tests", "any code change"),
            ("agent orchestration", "show wave plan", "launching agents"),
            ("staffing", "split by domain", "running agents in parallel"),
            ("review cadence", "code-review every PR", "merging to main"),
            ("rollout", "verify red", "writing implementation"),
            ("retrospective", "capture what went wrong", "ending the cycle"),
            ("evidence", "paste output", "claiming a phase complete"),
            ("planning", "announce the plan", "launching agents"),
        ],
    },
    {
        "slug": "incident-response",
        "type": "feedback",
        "topic_words": ["incident", "outage", "PSI", "page", "oncall", "runbook", "rollback"],
        "description_template": "Incident response: {} when {}",
        "phrasings": [
            ("lead with a runnable artifact", "the user is paged at 2am"),
            ("rollback first", "production is melting"),
            ("isolate the blast radius", "you don't yet know the cause"),
            ("preserve evidence", "before nuking the bad pod"),
            ("query the audit log", "before suspecting code"),
            ("page the right oncall", "the primary doesn't ack in 10 minutes"),
            ("run the prepared SQL", "checking for orphaned rows"),
            ("note timestamps in the channel", "every state transition"),
            ("declare severity early", "not after the fix"),
            ("write the postmortem same week", "memory is freshest"),
        ],
    },
    {
        "slug": "memory-hygiene",
        "type": "feedback",
        "topic_words": ["memory", "candidates", "promote", "decay", "frontmatter", "lessons", "graduate"],
        "description_template": "Memory hygiene: {} so that {}",
        "phrasings": [
            ("write frontmatter for every lesson", "retrieval can rank by description weight"),
            ("decay candidates after 30 days", "the brain doesn't grow stale"),
            ("promote feedback before user", "the agent corrects course faster"),
            ("namespace personal vs project", "search filtering works"),
            ("keep MEMORY.md under 200 lines", "auto-load doesn't truncate"),
            ("reject candidates with no why", "future-you can judge edge cases"),
            ("dedup before promote", "the same lesson doesn't graduate twice"),
            ("preserve original phrasing", "retrieval matches the words you'd type"),
            ("scrub PII in candidates", "they can be safely git-synced"),
            ("review queue weekly", "candidates don't pile up forever"),
        ],
    },
    {
        "slug": "test-rigor",
        "type": "feedback",
        "topic_words": ["test", "TDD", "mock", "integration", "TestContainers", "Playwright", "real database"],
        "description_template": "Tests: {} not {}",
        "phrasings": [
            ("hit a real Postgres in integration tests", "mock the DB"),
            ("test the seam between A and B", "test each unit in isolation only"),
            ("read user-facing strings as a user", "trust the assertion alone"),
            ("name fields by what they store", "by what callers initially want"),
            ("use TestContainers for ephemeral DBs", "spin up a permanent test cluster"),
            ("Playwright by role and label", "Playwright by CSS selector"),
            ("MSW for fetch interception", "global fetch stubs in setupTests"),
            ("snapshot the API contract", "manually compare JSON in PRs"),
            ("run the full E2E nightly", "only on PR open"),
            ("make tests deterministic", "rely on time.now() in assertions"),
        ],
    },
    {
        "slug": "data-vs-ai",
        "type": "feedback",
        "topic_words": ["clustering", "data", "CSV", "ground truth", "AI", "LLM", "delegate"],
        "description_template": "Prefer {} over {}",
        "phrasings": [
            ("authoritative org CSVs", "asking the agent to cluster from scratch"),
            ("a SQL query against the source of truth", "the agent's recall of the schema"),
            ("the runbook the team already wrote", "the agent inventing one from snippets"),
            ("the deploy log", "the agent's guess at what changed"),
            ("a known-good fixture", "agent-generated test data"),
            ("ground-truth labels from the dashboard", "agent-inferred categories"),
            ("an existing sklearn baseline", "a freshly-prompted classifier"),
            ("the schema diff in git", "a regenerated schema dump"),
            ("the actual error from logs", "the agent's reproduction of it"),
            ("a script the human wrote", "a shell pipeline the agent improvises"),
        ],
    },
    {
        "slug": "permission-denials",
        "type": "feedback",
        "topic_words": ["permission", "deny", "scope", "blast radius", "destructive", "force", "confirm"],
        "description_template": "Permission denials are signal: {}",
        "phrasings": [
            ("when the user denies a tool, ask what they meant",),
            ("a force-push refusal usually means rebase the branch instead",),
            ("if the user blocks `rm -rf`, surface the alternative",),
            ("destructive ops on shared state need explicit yes",),
            ("don't retry the same denied call with a flag",),
            ("treat repeated denies as a redirection, not noise",),
            ("ask before `git reset --hard` even with auto mode on",),
            ("never `--no-verify` past a hook denial",),
            ("a missing scope on a token is a config issue not a code issue",),
            ("don't escalate to root without a written authorization",),
        ],
    },
    {
        "slug": "cli-edge-cases",
        "type": "reference",
        "topic_words": ["CLI", "shell", "bash", "zsh", "quoting", "brackets", "argv"],
        "description_template": "CLI gotcha: {}",
        "phrasings": [
            ("brackets in vault names break some password CLIs; use UUIDs instead",),
            ("zsh treats `[` as a glob; quote any argument with brackets",),
            ("`xargs` on macOS doesn't accept `-r`; use `xargs -I {}` patterns",),
            ("`find -regex` alternation reads left-to-right; longest pattern first",),
            ("`tar -cz` preserves perms but not ACLs by default",),
            ("`rsync -aHAX` carries hardlinks, ACLs, xattrs",),
            ("`jq -r '.field // empty'` skips missing keys cleanly",),
            ("`grep -P` isn't on stock BSD grep; use `grep -E` with care",),
            ("`shellcheck` catches more than `set -euo pipefail`",),
            ("`mktemp -d` arguments differ between GNU and BSD",),
        ],
    },
    {
        "slug": "context-bloat",
        "type": "feedback",
        "topic_words": ["context", "bloat", "read", "auto-load", "MEMORY.md", "files", "200 lines"],
        "description_template": "Context bloat: {}",
        "phrasings": [
            ("don't Read whole files when grep would do",),
            ("MEMORY.md auto-load truncates after 200 lines; keep it lean",),
            ("paraphrased queries miss substring search; use a retriever",),
            ("agent re-reading the same file is a smell",),
            ("delegate broad searches to a subagent so the main thread stays clean",),
            ("short conftest beats long fixtures; collection time matters",),
            ("trim Read offsets to the lines you actually need",),
            ("notebook-first for cross-repo questions; cheaper than cloning",),
            ("don't paste full diffs into chat; summarize",),
            ("CLAUDE.md should be load-bearing facts, not aspirations",),
        ],
    },
]


@dataclass
class Lesson:
    slug: str  # filename stem
    name: str  # frontmatter name
    description: str
    type: str
    body: str
    bucket_slug: str


@dataclass
class EvalCase:
    query: str
    target_slug: str  # the lesson slug expected at top-K
    kind: str  # "lexical" or "paraphrase"


def _make_body(rng: random.Random, bucket: dict, phrasing: tuple, idx: int) -> str:
    topic = bucket["topic_words"]
    head = " ".join(phrasing) + "."
    sentences = [head]
    n_extra = rng.randint(3, 6)
    for _ in range(n_extra):
        ws = rng.sample(topic, k=min(3, len(topic)))
        sentences.append(
            f"This came up during {ws[0]} when the {ws[1]} pipeline produced unexpected {ws[2]}."
        )
    return " ".join(sentences)


_VERBS = [
    "avoid", "prefer", "ensure", "never use", "always check", "stop using",
    "lean on", "audit", "guard against", "prove",
]
_CONTEXTS = [
    "at scale", "under load", "in production", "during incidents", "after deploys",
    "on cold start", "in CI", "in staging", "during onboarding", "on legacy systems",
]
_QUALIFIERS = [
    "by default", "explicitly", "as a habit", "before merging", "as part of code review",
    "in the on-call runbook", "during retrospectives",
]


def _expanded_phrasings(bucket: dict, target_per_bucket: int, rng: random.Random) -> list[tuple]:
    """Combinatorial expansion of a bucket's seed phrasings.

    Each seed phrasing × verb × context × qualifier produces a distinct lesson.
    Falls back to bucket['phrasings'] when target_per_bucket <= len(phrasings).
    """
    seeds = bucket["phrasings"]
    if target_per_bucket <= len(seeds):
        return list(seeds[:target_per_bucket])
    expanded: list[tuple] = []
    for s in seeds:
        for v in _VERBS:
            for c in _CONTEXTS:
                for q in _QUALIFIERS:
                    if isinstance(s, tuple) and len(s) == 3:
                        expanded.append((f"{v} {s[0]}", f"{c}: {s[1]}", f"{q} — {s[2]}"))
                    elif isinstance(s, tuple) and len(s) == 2:
                        expanded.append((f"{v} {s[0]} {c}", f"{q} — {s[1]}"))
                    else:
                        expanded.append((f"{v} {s[0]} ({c}, {q})",))
    rng.shuffle(expanded)
    return expanded[:target_per_bucket]


def generate_corpus(seed: int = 42, target_size: int = 80) -> tuple[list[Lesson], list[EvalCase]]:
    rng = random.Random(seed)
    lessons: list[Lesson] = []
    eval_cases: list[EvalCase] = []

    n_buckets = len(BUCKETS)
    per_bucket = max(1, target_size // n_buckets)
    # Distribute remainder across the first few buckets so we hit target_size exactly
    remainder = target_size - per_bucket * n_buckets

    for bi, bucket in enumerate(BUCKETS):
        n = per_bucket + (1 if bi < remainder else 0)
        phrasings = _expanded_phrasings(bucket, n, rng)
        for i, phrasing in enumerate(phrasings):
            slug = f"{bucket['slug']}-{i+1:04d}"
            # Build the description
            if len(phrasing) == 3:
                description = bucket["description_template"].format(*phrasing)
            elif len(phrasing) == 2:
                description = bucket["description_template"].format(*phrasing)
            else:
                description = bucket["description_template"].format(phrasing[0])
            body = _make_body(rng, bucket, phrasing, i)
            lessons.append(
                Lesson(
                    slug=slug,
                    name=slug,
                    description=description,
                    type=bucket["type"],
                    body=body,
                    bucket_slug=bucket["slug"],
                )
            )

    # Build eval queries.
    # - Lexical: query reuses 2+ content words from a target's description.
    # - Paraphrase: query for a bucket as a whole (no shared words with any specific
    #   target's description). For paraphrase we *don't* tag a specific slug —
    #   bucket-recall is the meaningful metric. We tag any lesson in the bucket and
    #   score via bucket_for_slug at scoring time.
    # Scale eval set with corpus size, capped at 200 to keep bench runtime sane.
    lexical_count = min(200, max(20, target_size // 10))
    paraphrase_count = min(200, max(20, target_size // 10))

    chosen_lex = rng.sample(lessons, min(lexical_count, len(lessons)))
    for lesson in chosen_lex:
        words = [w for w in lesson.description.replace(":", " ").replace("—", " ").split() if len(w) > 3]
        if len(words) < 3:
            continue
        rng.shuffle(words)
        picked = words[:3]
        eval_cases.append(
            EvalCase(
                query="how to " + " ".join(picked),
                target_slug=lesson.slug,
                kind="lexical",
            )
        )

    paraphrase_map = {
        "agent-team-workflow": "what's the right way to coordinate parallel work between subagents",
        "incident-response": "production just went sideways at 2 in the morning, what now",
        "memory-hygiene": "my candidates folder keeps growing, how do I keep it from getting messy",
        "test-rigor": "should I stub the database or run a real one for these checks",
        "data-vs-ai": "I have a CSV of categories, should I let the model bucket things instead",
        "permission-denials": "the assistant just refused my command, what's the next step",
        "cli-edge-cases": "the password manager keeps choking on a vault name with square brackets",
        "context-bloat": "the agent keeps reading huge files I don't need, what should I change",
    }
    # For each bucket, generate `paraphrase_count // n_buckets` paraphrase queries pointing at
    # any lesson in that bucket (target_slug only used as a hint; scoring uses bucket recall).
    per_bucket_paraphrase = max(1, paraphrase_count // n_buckets)
    for bucket in BUCKETS:
        bucket_lessons = [l for l in lessons if l.bucket_slug == bucket["slug"]]
        if not bucket_lessons:
            continue
        base_query = paraphrase_map.get(bucket["slug"])
        if base_query is None:
            continue
        for j in range(per_bucket_paraphrase):
            target = rng.choice(bucket_lessons)
            # For variety across multiple per-bucket queries, lightly tweak the base query
            tweaks = ["", " right now", " in this codebase", " for our team", " honestly"]
            q = base_query + tweaks[j % len(tweaks)]
            eval_cases.append(EvalCase(query=q, target_slug=target.slug, kind="paraphrase"))

    return lessons, eval_cases


# ---------------------------------------------------------------------------
# Brain layout writer
# ---------------------------------------------------------------------------


def write_brain(brain_root: Path, lessons: list[Lesson]) -> None:
    """Write the synthetic lessons into $BRAIN_ROOT/memory/semantic/lessons/
    and produce a MEMORY.md index that mirrors the auto-memory format.
    """
    memory = brain_root / "memory"
    semantic = memory / "semantic" / "lessons"
    episodic = memory / "episodic"
    working = memory / "working"
    candidates = memory / "candidates"
    for d in (semantic, episodic, working, candidates):
        d.mkdir(parents=True, exist_ok=True)

    # Write each lesson as a frontmatter-prefixed .md file
    for lesson in lessons:
        path = semantic / f"{lesson.slug}.md"
        # Frontmatter values must be quoted to avoid YAML colon-parsing surprises
        content = (
            "---\n"
            f"name: {lesson.name}\n"
            f'description: "{lesson.description}"\n'
            f"type: {lesson.type}\n"
            "---\n"
            f"{lesson.body}\n"
        )
        path.write_text(content, encoding="utf-8")

    # MEMORY.md: list every lesson with its description (mirrors how auto-memory
    # framework writes the index — used by the without-recall baselines)
    memory_md = ["# Memory Index", ""]
    for lesson in lessons:
        link = f"semantic/lessons/{lesson.slug}.md"
        memory_md.append(f"- [{lesson.name}]({link}) — {lesson.description}")
    (memory / "MEMORY.md").write_text("\n".join(memory_md) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Retrieval strategies
# ---------------------------------------------------------------------------


def _normalize(s: str) -> set[str]:
    return {w.lower() for w in s.replace("/", " ").replace(":", " ").split() if len(w) > 2}


_MEMORY_MD_AUTOLOAD_LINES = 200  # Claude Code's default auto-load truncation


def retrieve_index_only(brain_root: Path, query: str, k: int = 5, max_lines: Optional[int] = None) -> list[str]:
    """Without-recall: substring match on MEMORY.md description column.

    If `max_lines` is set, only the first `max_lines` of MEMORY.md are visible —
    simulates Claude Code's auto-load truncation at scale.
    """
    memory_md = (brain_root / "memory" / "MEMORY.md").read_text(encoding="utf-8")
    if max_lines is not None:
        memory_md = "\n".join(memory_md.splitlines()[:max_lines])
    qwords = _normalize(query)
    scored: list[tuple[int, str]] = []
    for line in memory_md.splitlines():
        if not line.startswith("- ["):
            continue
        # Format: - [name](path) — description
        try:
            slug = line.split("](")[0].lstrip("- [")
            description = line.split(" — ", 1)[1]
        except IndexError:
            continue
        dwords = _normalize(description)
        overlap = len(qwords & dwords)
        if overlap > 0:
            scored.append((overlap, slug))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [slug for _, slug in scored[:k]]


def retrieve_index_plus_reads(brain_root: Path, query: str, k: int = 5, read_top_n: int = 10) -> list[str]:
    """Without-recall: index-only top-N, then read each candidate's body and
    rerank by full-body overlap. Mimics what a careful agent does today.
    """
    memory_md = (brain_root / "memory" / "MEMORY.md").read_text(encoding="utf-8")
    qwords = _normalize(query)
    scored: list[tuple[int, str, Path]] = []
    for line in memory_md.splitlines():
        if not line.startswith("- ["):
            continue
        try:
            slug = line.split("](")[0].lstrip("- [")
            link = line.split("](")[1].split(")")[0]
            description = line.split(" — ", 1)[1]
        except IndexError:
            continue
        dwords = _normalize(description)
        overlap = len(qwords & dwords)
        scored.append((overlap, slug, brain_root / "memory" / link))
    scored.sort(key=lambda x: (-x[0], x[1]))
    candidates = scored[:read_top_n]

    # Read each body and rerank by combined description+body overlap
    reranked: list[tuple[int, str]] = []
    for _, slug, path in candidates:
        try:
            body = path.read_text(encoding="utf-8")
        except OSError:
            continue
        bwords = _normalize(body)
        overlap = len(qwords & bwords)
        reranked.append((overlap, slug))
    reranked.sort(key=lambda x: (-x[0], x[1]))
    return [slug for _, slug in reranked[:k]]


def build_recall_retriever(brain_root: Path, use_embeddings: bool):
    """Build a HybridRetriever once. Production callers (`recall query`) reuse
    the on-disk cache; for the bench we just hold the in-memory retriever and
    reuse it across queries — gives the warm-cache numbers users actually see.
    """
    from recall.config import SourceConfig
    from recall.core import HybridRetriever
    from recall.sources import discover_documents

    source = SourceConfig(
        name="brain",
        path=str(brain_root / "memory"),
        glob="**/*.md",
        frontmatter="auto-memory",
        exclude=["episodic/**", "candidates/**", "working/**"],
    )
    docs = list(discover_documents(source))
    return HybridRetriever(
        docs,
        bm25_weight=1.0,
        embedding_weight=1.0 if use_embeddings else 0.0,
        embedding_model="all-MiniLM-L6-v2",
    )


def retrieve_with_recall_warm(retriever, query: str, k: int):
    """Warm-cache: query an already-constructed retriever."""
    results = retriever.query(query, k=k)
    slugs = []
    for r in results:
        doc = r.document
        slugs.append(doc.frontmatter.get("name") or Path(doc.path).stem)
    return slugs


# ---------------------------------------------------------------------------
# Bench loop
# ---------------------------------------------------------------------------


def bench_strategy_split(
    name: str,
    fn,
    eval_cases: list[EvalCase],
    bucket_for_slug: dict[str, str],
    k: int = 5,
) -> dict:
    """Run `fn(query, k)` against every eval case; report:
    - recall_at_k (slug-exact)
    - bucket_recall_at_k (any lesson from the right bucket counts as a hit)
    - per-kind splits for both
    - latency p50/p95
    """
    latencies_ms: list[float] = []
    slug_hits = 0
    bucket_hits = 0
    slug_lex = bucket_lex = slug_para = bucket_para = 0
    n_lex = n_para = 0
    for case in eval_cases:
        t0 = time.perf_counter()
        slugs = fn(case.query, k)
        latencies_ms.append((time.perf_counter() - t0) * 1000.0)

        slug_hit = case.target_slug in slugs
        target_bucket = bucket_for_slug.get(case.target_slug, "")
        retrieved_buckets = {bucket_for_slug.get(s, "") for s in slugs}
        bucket_hit = target_bucket in retrieved_buckets

        slug_hits += int(slug_hit)
        bucket_hits += int(bucket_hit)
        if case.kind == "lexical":
            n_lex += 1
            slug_lex += int(slug_hit)
            bucket_lex += int(bucket_hit)
        else:
            n_para += 1
            slug_para += int(slug_hit)
            bucket_para += int(bucket_hit)

    latencies_ms.sort()

    def pct(p: float) -> float:
        idx = max(0, min(len(latencies_ms) - 1, int(round(p / 100 * len(latencies_ms))) - 1))
        return latencies_ms[idx]

    n = len(eval_cases)
    return {
        "name": name,
        "recall_at_k": slug_hits / n,
        "bucket_recall_at_k": bucket_hits / n,
        "recall_lexical": slug_lex / max(1, n_lex),
        "bucket_lexical": bucket_lex / max(1, n_lex),
        "recall_paraphrase": slug_para / max(1, n_para),
        "bucket_paraphrase": bucket_para / max(1, n_para),
        "p50_ms": statistics.median(latencies_ms),
        "p95_ms": pct(95),
        "n": n,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="store_true", help="Print markdown table")
    parser.add_argument("--keep-brain", action="store_true", help="Don't clean up the synthetic brain")
    parser.add_argument(
        "--brain-root",
        default=None,
        help="Use this BRAIN_ROOT instead of a tempdir (preserves the synthetic brain)",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument(
        "--scale",
        type=int,
        default=80,
        help="Total number of synthetic lessons (defaults to 80; try 1000, 5000)",
    )
    parser.add_argument(
        "--json",
        type=str,
        default=None,
        help="Write machine-readable results JSON to this path (in addition to stdout report)",
    )
    args = parser.parse_args()

    if args.brain_root:
        brain_root = Path(args.brain_root)
        brain_root.mkdir(parents=True, exist_ok=True)
        for child in ("memory",):
            target = brain_root / child
            if target.exists():
                shutil.rmtree(target)
        cleanup = False
    else:
        tmp = tempfile.mkdtemp(prefix="brainstack-bench-")
        brain_root = Path(tmp)
        cleanup = not args.keep_brain

    print(f"Brain root: {brain_root}", flush=True)

    lessons, eval_cases = generate_corpus(seed=args.seed, target_size=args.scale)
    print(f"Generated {len(lessons)} lessons, {len(eval_cases)} eval cases "
          f"({sum(1 for c in eval_cases if c.kind == 'lexical')} lexical, "
          f"{sum(1 for c in eval_cases if c.kind == 'paraphrase')} paraphrase)", flush=True)

    write_brain(brain_root, lessons)

    bucket_for_slug = {l.slug: l.bucket_slug for l in lessons}

    # Without-recall strategies (closures over brain_root)
    def s_index_only(q, k):
        return retrieve_index_only(brain_root, q, k)

    def s_index_only_truncated(q, k):
        # Simulates Claude Code's MEMORY.md auto-load cap (~200 lines).
        return retrieve_index_only(brain_root, q, k, max_lines=_MEMORY_MD_AUTOLOAD_LINES)

    def s_index_reads(q, k):
        return retrieve_index_plus_reads(brain_root, q, k)

    print("Running strategies (this loads the embedding model on first call)...", flush=True)
    results = []
    results.append(
        bench_strategy_split(
            "Without recall (index, truncated 200 lines)",
            s_index_only_truncated,
            eval_cases,
            bucket_for_slug,
            k=args.k,
        )
    )
    print(f"  ✓ {results[-1]['name']}", flush=True)
    results.append(
        bench_strategy_split(
            "Without recall (index, full)", s_index_only, eval_cases, bucket_for_slug, k=args.k
        )
    )
    print(f"  ✓ {results[-1]['name']}", flush=True)
    results.append(
        bench_strategy_split(
            "Without recall (index + reads, full)", s_index_reads, eval_cases, bucket_for_slug, k=args.k
        )
    )
    print(f"  ✓ {results[-1]['name']}", flush=True)

    # With-recall: build retriever ONCE per mode, then query — mirrors how `recall query`
    # works in production (reuses the on-disk cache across calls).
    print("  building BM25-only retriever...", flush=True)
    r_bm25 = build_recall_retriever(brain_root, use_embeddings=False)
    s_recall_bm25 = lambda q, k: retrieve_with_recall_warm(r_bm25, q, k)
    results.append(
        bench_strategy_split(
            "With recall (BM25-only, warm)", s_recall_bm25, eval_cases, bucket_for_slug, k=args.k
        )
    )
    print(f"  ✓ {results[-1]['name']}", flush=True)

    print("  building hybrid retriever (loads MiniLM ~90 MB on first call)...", flush=True)
    r_hybrid = build_recall_retriever(brain_root, use_embeddings=True)
    s_recall_hybrid = lambda q, k: retrieve_with_recall_warm(r_hybrid, q, k)
    results.append(
        bench_strategy_split(
            "With recall (hybrid, warm)", s_recall_hybrid, eval_cases, bucket_for_slug, k=args.k
        )
    )
    print(f"  ✓ {results[-1]['name']}", flush=True)

    # Report
    if args.report:
        print()
        print(f"## Retrieval benchmark — synthetic brainstack brain ({len(lessons)} lessons, {len(eval_cases)} queries)")
        print()
        print("### Slug-exact recall@5 (the eval target was a specific lesson)")
        print()
        print(f"| Strategy | Overall | Lexical | Paraphrase | p50 ms | p95 ms |")
        print("|---|---|---|---|---|---|")
        for r in results:
            print(
                f"| {r['name']} | {r['recall_at_k']:.0%} | {r['recall_lexical']:.0%} | "
                f"{r['recall_paraphrase']:.0%} | {r['p50_ms']:.1f} | {r['p95_ms']:.1f} |"
            )
        print()
        print("### Bucket-recall@5 (any lesson from the right conceptual bucket counts as a hit)")
        print()
        print("This is closer to real use: when the user asks 'production is on fire at 2am',")
        print("they want any incident-response lesson, not necessarily lesson #01 specifically.")
        print()
        print("| Strategy | Overall | Lexical | Paraphrase | p50 ms | p95 ms |")
        print("|---|---|---|---|---|---|")
        for r in results:
            print(
                f"| {r['name']} | {r['bucket_recall_at_k']:.0%} | {r['bucket_lexical']:.0%} | "
                f"{r['bucket_paraphrase']:.0%} | {r['p50_ms']:.1f} | {r['p95_ms']:.1f} |"
            )
        print()
        print("### Notes")
        print("- Latency is per-query wall clock for the retrieval step itself, warm-cache for")
        print("  recall (mirrors what `recall query` does in production after `recall reindex`).")
        print("- 'index + reads' reads up to 10 file bodies per query.")
        print("- Hybrid latency includes the MiniLM forward pass per query.")
        print()

    # Machine-readable JSON output
    if args.json:
        payload = {
            "scale": args.scale,
            "seed": args.seed,
            "k": args.k,
            "n_lessons": len(lessons),
            "n_eval_cases": len(eval_cases),
            "results": results,
        }
        Path(args.json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Wrote JSON results to {args.json}", flush=True)

    if cleanup:
        shutil.rmtree(brain_root, ignore_errors=True)
        print(f"Cleaned up {brain_root}", flush=True)
    else:
        print(f"Brain preserved at {brain_root}", flush=True)


if __name__ == "__main__":
    main()
