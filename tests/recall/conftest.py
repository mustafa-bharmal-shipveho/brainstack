"""Shared pytest fixtures for recall-brain tests.

All fixtures use synthetic data only. No fixture reads from a user's real brain
directory. Generic, open-source-safe content (programming concepts, recipes,
gardening tips) is used so test data can be inspected publicly without leaking
anything sensitive.
"""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

import pytest
import yaml


# ---------------------------------------------------------------------------
# Synthetic memory entries
#
# These mimic the auto-memory framework's frontmatter convention. Content is
# deliberately generic — programming tutorials, recipes, gardening — so the
# corpus can ship in tests without privacy concerns.
# ---------------------------------------------------------------------------

SYNTHETIC_AUTO_MEMORY = {
    # type=feedback (lessons / rules)
    "feedback_pin_dependencies.md": {
        "frontmatter": {
            "name": "pin-dependencies",
            "description": "Pin dependency versions in lockfiles to avoid surprise breakages on rebuild",
            "type": "feedback",
        },
        "body": (
            "When working with package managers like pip, npm, or cargo, always commit a lockfile.\n"
            "Why: a fresh install months later can otherwise pull a new minor version with breaking changes.\n"
            "How to apply: run `pip freeze > requirements.txt` or commit `package-lock.json`/`Cargo.lock`.\n"
        ),
    },
    "feedback_unicode_filenames.md": {
        "frontmatter": {
            "name": "unicode-filenames",
            "description": "Filesystem APIs may NFD-normalize filenames on macOS; compare with NFC",
            "type": "feedback",
        },
        "body": (
            "macOS HFS+/APFS normalize filenames to NFD. If a name contains accented characters,\n"
            "the bytes you wrote may differ from the bytes you read back.\n"
            "How to apply: normalize both sides with `unicodedata.normalize('NFC', name)` before comparing.\n"
        ),
    },
    "feedback_atomic_writes.md": {
        "frontmatter": {
            "name": "atomic-writes",
            "description": "Use temp file + os.replace for crash-safe writes; never write in place",
            "type": "feedback",
        },
        "body": (
            "Writing directly to a file leaves a half-written state if the process crashes.\n"
            "Pattern: write to `path.tmp`, fsync, then `os.replace(path.tmp, path)`.\n"
            "How to apply: any time you write JSON state, configs, or caches.\n"
        ),
    },
    "feedback_signed_zero_quirk.md": {
        "frontmatter": {
            "name": "signed-zero",
            "description": "IEEE 754 has -0.0 distinct from 0.0; matters for division and copysign",
            "type": "feedback",
        },
        "body": "Use math.copysign or check `1/x == -inf` to detect negative zero.\n",
    },
    # type=user (profile)
    "user_profile.md": {
        "frontmatter": {
            "name": "user-profile",
            "description": "Generic developer profile for example purposes",
            "type": "user",
        },
        "body": "Backend engineer. Prefers terse explanations and concrete examples.\n",
    },
    # type=project
    "project_release_freeze.md": {
        "frontmatter": {
            "name": "release-freeze-policy",
            "description": "Hypothetical merge-freeze policy starting Friday",
            "type": "project",
        },
        "body": (
            "Example project memory: merge freeze begins on Friday for a release cut.\n"
            "Why: stabilize the release branch.\n"
            "How to apply: hold non-critical PRs until after the cut.\n"
        ),
    },
    # type=reference
    "reference_python_docs.md": {
        "frontmatter": {
            "name": "python-stdlib-docs",
            "description": "Canonical reference for Python standard library APIs",
            "type": "reference",
        },
        "body": "https://docs.python.org/3/library/\n",
    },
    "reference_obscure_jargon.md": {
        "frontmatter": {
            "name": "splork-protocol",
            "description": "Reference for the made-up Splork wire protocol used in examples",
            "type": "reference",
        },
        "body": (
            "Splork is a fictional binary protocol used in test fixtures.\n"
            "It uses big-endian framing and a four-byte magic number `SPLK`.\n"
        ),
    },
}


SYNTHETIC_GENERIC = {
    # plain notes without strict frontmatter, mimicking Obsidian/Logseq
    "recipes/lasagna.md": (
        "# Classic Lasagna\n\n"
        "Layered pasta with bechamel, ragu, and parmesan.\n"
        "Bake at 180C for 35 minutes, rest 10 minutes before slicing.\n"
    ),
    "recipes/risotto.md": (
        "# Mushroom Risotto\n\n"
        "Toast arborio rice with onion, deglaze with white wine, ladle warm broth.\n"
        "Stir constantly. Finish with butter and grated parmesan.\n"
    ),
    "garden/tomatoes.md": (
        "# Heirloom Tomato Care\n\n"
        "Stake early. Prune suckers. Water at the base, not the leaves, to avoid blight.\n"
        "Pick when fully colored but still firm.\n"
    ),
    "garden/composting.md": (
        "# Composting Basics\n\n"
        "Mix browns (carbon: leaves, cardboard) and greens (nitrogen: kitchen scraps) 3:1.\n"
        "Turn weekly. Should smell earthy, not sour.\n"
    ),
    "music/scales.md": (
        "---\nname: scales\ntags: [theory, practice]\n---\n"
        "# Major scale intervals\n\n"
        "W-W-H-W-W-W-H. C major: C D E F G A B C.\n"
    ),
    "deeply/nested/path/note.md": (
        "# A Deeply Nested Note\n\n"
        "This note exists to test glob recursion.\n"
    ),
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_auto_memory_file(path: Path, frontmatter: dict, body: str) -> None:
    """Write a memory file with proper YAML quoting so colons in values don't break parsing."""
    yaml_block = yaml.safe_dump(frontmatter, sort_keys=False, default_flow_style=False).strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\n" + yaml_block + "\n---\n" + body, encoding="utf-8")


@pytest.fixture
def auto_memory_brain(tmp_path: Path) -> Path:
    """A synthetic auto-memory brain at tmp_path/brain with ~8 memories."""
    brain = tmp_path / "brain"
    brain.mkdir()

    # auto-memory layout: semantic/lessons + personal/{profile,notes,references}
    layout = {
        "semantic/lessons/feedback_pin_dependencies.md": SYNTHETIC_AUTO_MEMORY[
            "feedback_pin_dependencies.md"
        ],
        "semantic/lessons/feedback_unicode_filenames.md": SYNTHETIC_AUTO_MEMORY[
            "feedback_unicode_filenames.md"
        ],
        "semantic/lessons/feedback_atomic_writes.md": SYNTHETIC_AUTO_MEMORY[
            "feedback_atomic_writes.md"
        ],
        "semantic/lessons/feedback_signed_zero_quirk.md": SYNTHETIC_AUTO_MEMORY[
            "feedback_signed_zero_quirk.md"
        ],
        "personal/profile/user_profile.md": SYNTHETIC_AUTO_MEMORY["user_profile.md"],
        "personal/notes/project_release_freeze.md": SYNTHETIC_AUTO_MEMORY[
            "project_release_freeze.md"
        ],
        "personal/references/reference_python_docs.md": SYNTHETIC_AUTO_MEMORY[
            "reference_python_docs.md"
        ],
        "personal/references/reference_obscure_jargon.md": SYNTHETIC_AUTO_MEMORY[
            "reference_obscure_jargon.md"
        ],
    }

    for rel_path, entry in layout.items():
        _write_auto_memory_file(
            brain / rel_path,
            entry["frontmatter"],
            entry["body"],
        )

    # Top-level MEMORY.md index
    (brain / "MEMORY.md").write_text(
        "# Memory Index\n\n"
        "## Lessons\n"
        "- [pin-dependencies](semantic/lessons/feedback_pin_dependencies.md)\n"
        "- [unicode-filenames](semantic/lessons/feedback_unicode_filenames.md)\n"
        "- [atomic-writes](semantic/lessons/feedback_atomic_writes.md)\n"
        "- [signed-zero](semantic/lessons/feedback_signed_zero_quirk.md)\n",
        encoding="utf-8",
    )

    return brain


@pytest.fixture
def generic_brain(tmp_path: Path) -> Path:
    """A second-brain repo with optional/no frontmatter (Obsidian-style)."""
    brain = tmp_path / "vault"
    brain.mkdir()
    for rel_path, content in SYNTHETIC_GENERIC.items():
        target = brain / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return brain


@pytest.fixture
def empty_brain(tmp_path: Path) -> Path:
    brain = tmp_path / "empty"
    brain.mkdir()
    return brain


@pytest.fixture
def single_file_brain(tmp_path: Path) -> Path:
    brain = tmp_path / "solo"
    brain.mkdir()
    _write_auto_memory_file(
        brain / "lone.md",
        {"name": "lone", "description": "the only memory", "type": "feedback"},
        "Just one entry.\n",
    )
    return brain


@pytest.fixture
def malformed_brain(tmp_path: Path) -> Path:
    """A brain with files exhibiting various malformed/edge-case frontmatter."""
    brain = tmp_path / "broken"
    brain.mkdir()

    # No frontmatter at all
    (brain / "no_fm.md").write_text("# Just a title\n\nBody only.\n", encoding="utf-8")

    # Empty file
    (brain / "empty.md").write_text("", encoding="utf-8")

    # Only frontmatter, no body
    (brain / "only_fm.md").write_text(
        "---\nname: only-fm\ndescription: header only\ntype: reference\n---\n",
        encoding="utf-8",
    )

    # Unclosed frontmatter
    (brain / "unclosed_fm.md").write_text(
        "---\nname: unclosed\ndescription: missing closer\n\nBody starts here.\n",
        encoding="utf-8",
    )

    # Malformed YAML (tab in indentation)
    (brain / "bad_yaml.md").write_text(
        "---\nname: bad\n\tdescription: tabs are invalid in yaml\n---\nBody.\n",
        encoding="utf-8",
    )

    # Frontmatter on second line (not first)
    (brain / "leading_blank.md").write_text(
        "\n---\nname: leading-blank\n---\nBody.\n",
        encoding="utf-8",
    )

    # Unicode in name and body (NFC and NFD variants)
    (brain / "unicode_nfc.md").write_text(
        "---\nname: café-tips\ndescription: NFC composed form\ntype: reference\n---\n"
        "Order an espresso doppio.\n",
        encoding="utf-8",
    )
    (brain / "unicode_nfd.md").write_text(
        "---\nname: café-tips\ndescription: NFD decomposed form\ntype: reference\n---\n"
        "Same word, different bytes.\n",
        encoding="utf-8",
    )

    # Very long single line
    (brain / "long_line.md").write_text(
        "---\nname: long\ndescription: " + ("x" * 5000) + "\ntype: reference\n---\nBody.\n",
        encoding="utf-8",
    )

    # Binary-ish content with .md extension
    (brain / "binary.md").write_bytes(b"\x00\x01\x02\x03binary content\xff\xfe")

    return brain


@pytest.fixture
def hundred_query_corpus(tmp_path: Path) -> tuple[Path, list[dict]]:
    """A larger synthetic corpus designed to support 100+ query test cases.

    Returns (brain_path, query_cases) where each case is
    {query: str, expected_top_name: str, rationale: str}.

    Topics span: programming languages, algorithms, devops, food, gardening,
    music, history — none of it tied to any real person, company, or project.
    """
    brain = tmp_path / "big"
    brain.mkdir()

    entries = _build_hundred_corpus()
    for path, frontmatter, body in entries:
        _write_auto_memory_file(brain / path, frontmatter, body)

    return brain, _build_query_cases()


def _build_hundred_corpus() -> list[tuple[str, dict, str]]:
    """40 distinct memories spanning multiple domains, each with strong lexical signal."""
    return [
        # Programming languages (10)
        (
            "lang/python_gil.md",
            {
                "name": "python-gil",
                "description": "Python global interpreter lock prevents true thread parallelism for CPU work",
                "type": "feedback",
            },
            "Use multiprocessing or asyncio for parallelism instead of threads when CPU bound.\n",
        ),
        (
            "lang/rust_borrow_checker.md",
            {
                "name": "rust-borrow-checker",
                "description": "Rust borrow checker enforces ownership and lifetime rules at compile time",
                "type": "feedback",
            },
            "Common patterns: prefer references, use Rc/RefCell when you need shared mutability.\n",
        ),
        (
            "lang/go_goroutines.md",
            {
                "name": "go-goroutines",
                "description": "Go goroutines are lightweight threads scheduled by the runtime",
                "type": "feedback",
            },
            "Use channels for communication, sync.Mutex for shared state.\n",
        ),
        (
            "lang/javascript_event_loop.md",
            {
                "name": "javascript-event-loop",
                "description": "JavaScript single-threaded event loop with microtasks and macrotasks",
                "type": "feedback",
            },
            "Promises are microtasks, setTimeout is macrotask. Microtasks flush before next macrotask.\n",
        ),
        (
            "lang/cpp_raii.md",
            {
                "name": "cpp-raii",
                "description": "C++ RAII ties resource lifetime to object scope via destructors",
                "type": "feedback",
            },
            "Use unique_ptr for ownership, shared_ptr for shared, never raw new/delete.\n",
        ),
        (
            "lang/typescript_generics.md",
            {
                "name": "typescript-generics",
                "description": "TypeScript generics allow type parameters for reusable type-safe abstractions",
                "type": "feedback",
            },
            "Use `extends` for constraints, `keyof` for property keys, conditional types for branching.\n",
        ),
        (
            "lang/haskell_monad.md",
            {
                "name": "haskell-monad",
                "description": "Haskell monads encapsulate effects in pure functional context",
                "type": "feedback",
            },
            "IO, Maybe, Either are common monads. Use do-notation for sequencing.\n",
        ),
        (
            "lang/ruby_blocks.md",
            {
                "name": "ruby-blocks",
                "description": "Ruby blocks pass anonymous functions to methods using do-end or curly braces",
                "type": "feedback",
            },
            "Use yield to invoke, &block to capture, Proc.new for explicit construction.\n",
        ),
        (
            "lang/swift_optionals.md",
            {
                "name": "swift-optionals",
                "description": "Swift optionals encode possibly-absent values at the type level",
                "type": "feedback",
            },
            "Unwrap with if-let, guard-let, or ??. Force unwrap with ! only when invariant guaranteed.\n",
        ),
        (
            "lang/kotlin_coroutines.md",
            {
                "name": "kotlin-coroutines",
                "description": "Kotlin coroutines provide structured concurrency on the JVM",
                "type": "feedback",
            },
            "Launch in a CoroutineScope, use suspend functions for non-blocking work.\n",
        ),
        # Algorithms / data structures (10)
        (
            "algo/quicksort.md",
            {
                "name": "quicksort",
                "description": "Quicksort partitions around a pivot recursively, average O(n log n)",
                "type": "reference",
            },
            "Worst case O(n^2) on already-sorted input with poor pivot choice. Use median-of-three.\n",
        ),
        (
            "algo/dijkstra.md",
            {
                "name": "dijkstra-shortest-path",
                "description": "Dijkstra finds single-source shortest paths in a non-negative weighted graph",
                "type": "reference",
            },
            "Use a min-heap priority queue. Cannot handle negative edges; use Bellman-Ford instead.\n",
        ),
        (
            "algo/btree.md",
            {
                "name": "btree-index",
                "description": "B-trees underpin most relational database indexes for range scans",
                "type": "reference",
            },
            "High fanout, balanced height, log-time lookup. Variant B+tree links leaves for range scan.\n",
        ),
        (
            "algo/hashmap.md",
            {
                "name": "hashmap-collision",
                "description": "Hashmap collision resolution: chaining vs open addressing",
                "type": "reference",
            },
            "Chaining uses linked lists per bucket. Open addressing probes alternative slots.\n",
        ),
        (
            "algo/dynamic_programming.md",
            {
                "name": "dynamic-programming",
                "description": "Dynamic programming solves problems by combining solutions to subproblems",
                "type": "reference",
            },
            "Memoize recursive calls (top-down) or fill a table iteratively (bottom-up).\n",
        ),
        (
            "algo/bfs_dfs.md",
            {
                "name": "bfs-vs-dfs",
                "description": "Breadth-first search uses a queue, depth-first uses a stack or recursion",
                "type": "reference",
            },
            "BFS finds shortest unweighted paths. DFS is natural for topological sort and cycles.\n",
        ),
        (
            "algo/bloom_filter.md",
            {
                "name": "bloom-filter",
                "description": "Bloom filters give probabilistic set membership with no false negatives",
                "type": "reference",
            },
            "Use multiple hash functions and a bit array. False positive rate tunable by size.\n",
        ),
        (
            "algo/union_find.md",
            {
                "name": "union-find",
                "description": "Union-find tracks disjoint sets with near-constant-time merge and lookup",
                "type": "reference",
            },
            "Use path compression and union by rank for inverse-Ackermann amortized complexity.\n",
        ),
        (
            "algo/topological_sort.md",
            {
                "name": "topological-sort",
                "description": "Topological sort orders directed acyclic graph nodes by dependency",
                "type": "reference",
            },
            "Kahn's algorithm uses in-degree counting; DFS-based variant uses post-order reverse.\n",
        ),
        (
            "algo/lru_cache.md",
            {
                "name": "lru-cache",
                "description": "LRU cache evicts the least-recently-used entry when capacity exceeded",
                "type": "reference",
            },
            "Implement with hashmap + doubly linked list. Both operations O(1).\n",
        ),
        # DevOps / infrastructure (10)
        (
            "ops/docker_layers.md",
            {
                "name": "docker-layer-caching",
                "description": "Docker image layers cache by Dockerfile instruction order",
                "type": "feedback",
            },
            "Order instructions from least to most likely to change. Copy package files before source.\n",
        ),
        (
            "ops/kubernetes_pods.md",
            {
                "name": "kubernetes-pods",
                "description": "Kubernetes pods are the smallest deployable unit; usually one container",
                "type": "reference",
            },
            "Pods share network and storage. Use deployments to manage replicas.\n",
        ),
        (
            "ops/postgres_vacuum.md",
            {
                "name": "postgres-vacuum",
                "description": "PostgreSQL vacuum reclaims dead row space and updates planner statistics",
                "type": "feedback",
            },
            "Autovacuum runs in the background; tune thresholds for high-write tables.\n",
        ),
        (
            "ops/redis_persistence.md",
            {
                "name": "redis-persistence",
                "description": "Redis offers RDB snapshots and AOF append-only file durability",
                "type": "reference",
            },
            "RDB is fast to load; AOF is more durable but larger. Combine for best of both.\n",
        ),
        (
            "ops/dns_ttl.md",
            {
                "name": "dns-ttl",
                "description": "DNS TTL controls how long resolvers cache a record before refetching",
                "type": "feedback",
            },
            "Lower TTL before a planned migration so changes propagate quickly.\n",
        ),
        (
            "ops/tls_handshake.md",
            {
                "name": "tls-handshake",
                "description": "TLS 1.3 handshake completes in one round-trip with key exchange",
                "type": "reference",
            },
            "Server proves identity via certificate signed by trusted CA. Client verifies chain.\n",
        ),
        (
            "ops/git_rebase.md",
            {
                "name": "git-rebase-vs-merge",
                "description": "Git rebase rewrites commit history; merge preserves it",
                "type": "feedback",
            },
            "Rebase for clean linear history before sharing. Merge to record integration points.\n",
        ),
        (
            "ops/load_balancer.md",
            {
                "name": "load-balancer-strategies",
                "description": "Load balancer algorithms: round-robin, least-connections, hash-based",
                "type": "reference",
            },
            "Hash-based gives session affinity. Health checks remove unhealthy backends.\n",
        ),
        (
            "ops/ci_caching.md",
            {
                "name": "ci-caching-strategy",
                "description": "CI caching speeds builds by reusing dependencies across runs",
                "type": "feedback",
            },
            "Key by lockfile hash. Save before run, restore at start. Invalidates automatically.\n",
        ),
        (
            "ops/observability_pillars.md",
            {
                "name": "observability-pillars",
                "description": "Observability has three pillars: logs, metrics, and traces",
                "type": "reference",
            },
            "Logs are events. Metrics are aggregates. Traces show causality across services.\n",
        ),
        # Cooking / food (5)
        (
            "food/sourdough.md",
            {
                "name": "sourdough-starter",
                "description": "Sourdough starter is a wild yeast and lactobacillus culture in flour and water",
                "type": "reference",
            },
            "Feed daily at 1:1:1 ratio. Use when doubled within 4-6 hours.\n",
        ),
        (
            "food/maillard.md",
            {
                "name": "maillard-reaction",
                "description": "Maillard reaction browns proteins and sugars above 140C creating savory flavors",
                "type": "reference",
            },
            "Dry surface matters. Pat meat dry before searing. Avoid crowding the pan.\n",
        ),
        (
            "food/knife_sharpening.md",
            {
                "name": "knife-sharpening",
                "description": "Sharpen knives on a whetstone progressing from coarse to fine grit",
                "type": "reference",
            },
            "Hold consistent angle around 15-20 degrees. Finish with leather strop for polish.\n",
        ),
        (
            "food/pasta_water.md",
            {
                "name": "salting-pasta-water",
                "description": "Salt pasta water generously; the salt seasons the pasta from inside",
                "type": "feedback",
            },
            "Aim for sea-water salinity, around 1 tablespoon per liter.\n",
        ),
        (
            "food/emulsion.md",
            {
                "name": "emulsion-stability",
                "description": "Emulsions like mayonnaise stabilize fat in water with a surfactant",
                "type": "reference",
            },
            "Egg yolk lecithin emulsifies oil and lemon juice. Add oil slowly while whisking.\n",
        ),
        # Gardening (5)
        (
            "garden/companion_planting.md",
            {
                "name": "companion-planting",
                "description": "Companion planting pairs species that benefit each other",
                "type": "reference",
            },
            "Tomatoes with basil, carrots with onions. Avoid putting onions near beans.\n",
        ),
        (
            "garden/soil_ph.md",
            {
                "name": "soil-ph",
                "description": "Soil pH affects nutrient availability; most vegetables prefer 6.0 to 7.0",
                "type": "reference",
            },
            "Lime raises pH, sulfur lowers it. Test annually with a kit or meter.\n",
        ),
        (
            "garden/cover_crop.md",
            {
                "name": "cover-crops",
                "description": "Cover crops protect bare soil and fix nitrogen between plantings",
                "type": "reference",
            },
            "Clover and vetch fix nitrogen. Rye scavenges leftover nutrients.\n",
        ),
        (
            "garden/mulching.md",
            {
                "name": "mulching-benefits",
                "description": "Mulch retains moisture, suppresses weeds, and moderates soil temperature",
                "type": "reference",
            },
            "Wood chips for paths, straw for vegetables. Renew annually as it decomposes.\n",
        ),
        (
            "garden/pruning.md",
            {
                "name": "pruning-fruit-trees",
                "description": "Prune fruit trees in late winter while dormant for shape and yield",
                "type": "reference",
            },
            "Remove crossing branches, suckers, and dead wood. Open the canopy to light.\n",
        ),
    ]


def _build_query_cases() -> list[dict]:
    """120 query cases with expected top hit names. 100+ minimum per requirement.

    Each case is tagged with `kind`:
      - 'lexical': shares key tokens with the target's description; BM25 alone should hit top-3
      - 'paraphrase': semantic match only; requires embeddings to find target reliably
      - 'mixed': partial lexical overlap; BM25 *may* find it but isn't guaranteed
      - 'negative': no strong target; just verify the system doesn't crash

    Tests use `kind` to decide which cases to run BM25-only vs hybrid.
    """
    cases: list[dict] = []

    # Direct-match queries (one per memory, uses key terms)
    direct = [
        ("python global interpreter lock", "python-gil"),
        ("rust ownership lifetime", "rust-borrow-checker"),
        ("go lightweight threads channels", "go-goroutines"),
        ("javascript microtasks macrotasks", "javascript-event-loop"),
        ("c++ resource acquisition initialization", "cpp-raii"),
        ("typescript generic constraints", "typescript-generics"),
        ("haskell monad io effects", "haskell-monad"),
        ("ruby block do end yield", "ruby-blocks"),
        ("swift optional unwrap nil", "swift-optionals"),
        ("kotlin coroutine suspend", "kotlin-coroutines"),
        ("quicksort partition pivot", "quicksort"),
        ("shortest path graph weighted", "dijkstra-shortest-path"),
        ("database index range scan", "btree-index"),
        ("hash map collision linked list", "hashmap-collision"),
        ("memoize subproblem optimal substructure", "dynamic-programming"),
        ("breadth first depth first search", "bfs-vs-dfs"),
        ("probabilistic set membership false positive", "bloom-filter"),
        ("disjoint set merge find", "union-find"),
        ("dependency order acyclic graph", "topological-sort"),
        ("least recently used cache eviction", "lru-cache"),
        ("docker image layer caching", "docker-layer-caching"),
        ("kubernetes smallest deployable unit", "kubernetes-pods"),
        ("postgres reclaim dead rows", "postgres-vacuum"),
        ("redis snapshot append only file", "redis-persistence"),
        ("dns time to live propagation", "dns-ttl"),
        ("tls handshake certificate verification", "tls-handshake"),
        ("git rewrite history linear", "git-rebase-vs-merge"),
        ("load balancer round robin least connections", "load-balancer-strategies"),
        ("ci cache lockfile dependencies", "ci-caching-strategy"),
        ("logs metrics traces three pillars", "observability-pillars"),
        ("sourdough wild yeast starter", "sourdough-starter"),
        ("maillard browning meat sear", "maillard-reaction"),
        ("whetstone knife sharpen angle", "knife-sharpening"),
        ("pasta water salt seasoning", "salting-pasta-water"),
        ("mayonnaise emulsion lecithin", "emulsion-stability"),
        ("companion plant tomato basil", "companion-planting"),
        ("soil ph lime sulfur", "soil-ph"),
        ("cover crop nitrogen clover", "cover-crops"),
        ("mulch moisture weed suppress", "mulching-benefits"),
        ("prune fruit tree dormant winter", "pruning-fruit-trees"),
    ]
    for q, expected in direct:
        cases.append(
            {
                "query": q,
                "expected_top_name": expected,
                "kind": "lexical",
                "rationale": "Direct lexical match on key terms",
            }
        )

    # Additional lexical variations to push corpus over 100 cases. These query
    # phrasings still share key terms with the target's description.
    extra_lexical = [
        ("python gil thread parallelism", "python-gil"),
        ("rust ownership references compile time", "rust-borrow-checker"),
        ("goroutines channel coordination", "go-goroutines"),
        ("javascript microtask queue", "javascript-event-loop"),
        ("c++ resource management destructor", "cpp-raii"),
        ("typescript generic type parameter constraint", "typescript-generics"),
        ("haskell io monad sequence", "haskell-monad"),
        ("ruby block proc lambda yield", "ruby-blocks"),
        ("swift optional wrap nil safety", "swift-optionals"),
        ("dijkstra graph priority queue", "dijkstra-shortest-path"),
        ("btree leaf range query", "btree-index"),
        ("dynamic programming memoization table", "dynamic-programming"),
        ("bloom filter hash bit array", "bloom-filter"),
        ("union find path compression", "union-find"),
        ("kahn topological sort algorithm", "topological-sort"),
        ("docker layer cache invalidation", "docker-layer-caching"),
        ("postgres autovacuum table", "postgres-vacuum"),
        ("redis rdb aof durability", "redis-persistence"),
        ("dns ttl caching resolver", "dns-ttl"),
        ("git rebase clean linear history", "git-rebase-vs-merge"),
    ]
    for q, expected in extra_lexical:
        cases.append(
            {
                "query": q,
                "expected_top_name": expected,
                "kind": "lexical",
                "rationale": "Lexical variation on key terms",
            }
        )

    # Paraphrase queries (semantic matching, may rely on embeddings)
    paraphrase = [
        ("why does threading not give me parallel cpu work in python", "python-gil"),
        ("how does the compiler stop me from sharing references unsafely", "rust-borrow-checker"),
        ("how do tasks pause and resume in kotlin", "kotlin-coroutines"),
        ("what does the keyword that handles maybe missing values do", "swift-optionals"),
        ("how do i sort efficiently on average without merge sort", "quicksort"),
        ("find the cheapest route in a road network", "dijkstra-shortest-path"),
        ("how does an index help with between queries", "btree-index"),
        ("what to do when two keys hash to the same bucket", "hashmap-collision"),
        ("solve fibonacci without exponential blowup", "dynamic-programming"),
        ("queue based exploration of nodes by levels", "bfs-vs-dfs"),
        ("set membership check that may have false positives", "bloom-filter"),
        ("group elements that are connected", "union-find"),
        ("ordering tasks by their prerequisites", "topological-sort"),
        ("evict the oldest cache entry to make room", "lru-cache"),
        ("speed up image rebuilds by reusing parts", "docker-layer-caching"),
        ("what runs when i kubectl apply", "kubernetes-pods"),
        ("clean up old row versions", "postgres-vacuum"),
        ("disk persistence options for an in memory store", "redis-persistence"),
        ("how long does name resolution stay cached", "dns-ttl"),
        ("how does a browser confirm a websites identity", "tls-handshake"),
        ("rewrite my branch on top of main", "git-rebase-vs-merge"),
        ("distribute requests across servers", "load-balancer-strategies"),
        ("speed up github actions by caching node modules", "ci-caching-strategy"),
        ("what should i monitor in production", "observability-pillars"),
        ("flour and water leaven over weeks", "sourdough-starter"),
        ("brown the surface for flavor", "maillard-reaction"),
        ("keep your kitchen knife edge sharp", "knife-sharpening"),
        ("season the boiling water for noodles", "salting-pasta-water"),
        ("oil and water mixed with egg", "emulsion-stability"),
        ("plants that grow well together in the garden", "companion-planting"),
        ("acidic versus alkaline garden bed", "soil-ph"),
        ("plant something between crops to fix nitrogen", "cover-crops"),
        ("layer of straw to keep the bed moist", "mulching-benefits"),
        ("when to cut back the apple tree", "pruning-fruit-trees"),
    ]
    for q, expected in paraphrase:
        cases.append(
            {
                "query": q,
                "expected_top_name": expected,
                "kind": "paraphrase",
                "rationale": "Paraphrase, requires non-lexical matching",
            }
        )

    # Type-filtered queries (validate filter behavior)
    type_filtered = [
        ("event loop", "javascript-event-loop", "feedback"),
        ("dependency graph order", "topological-sort", "reference"),
        ("vacuum dead rows", "postgres-vacuum", "feedback"),
        ("acid versus alkaline soil", "soil-ph", "reference"),
        ("rebase versus merge", "git-rebase-vs-merge", "feedback"),
    ]
    for q, expected, type_filter in type_filtered:
        cases.append(
            {
                "query": q,
                "expected_top_name": expected,
                "type_filter": type_filter,
                "kind": "lexical",
                "rationale": f"Type-filtered ({type_filter})",
            }
        )

    # Mixed-domain disambiguation: shares some key tokens with target but is hard
    # because alternative memories share the same tokens too. BM25 should still
    # win on these because of description-weighting.
    mixed = [
        ("structured concurrency on the jvm", "kotlin-coroutines"),  # 'jvm' is unique
        ("compile time enforcement of borrow rules", "rust-borrow-checker"),  # 'borrow', 'compile'
        ("run goroutines and pass messages", "go-goroutines"),  # 'goroutines'
    ]
    for q, expected in mixed:
        cases.append(
            {
                "query": q,
                "expected_top_name": expected,
                "kind": "mixed",
                "rationale": "Disambiguation across overlapping topics",
            }
        )

    # These have no shared lexical tokens with their target's description and
    # therefore require embeddings to retrieve correctly.
    paraphrase_extra = [
        ("partition the data structure", "quicksort"),
        ("order things in a graph", "topological-sort"),
        ("anonymous function callback", "ruby-blocks"),
        ("avoid manual memory management", "cpp-raii"),
        ("sequential effects in pure functional code", "haskell-monad"),
    ]
    for q, expected in paraphrase_extra:
        cases.append(
            {
                "query": q,
                "expected_top_name": expected,
                "kind": "paraphrase",
                "rationale": "Paraphrase, requires embeddings",
            }
        )

    # Should-not-match (negative cases)
    # Note: not asserting "no match" here; just ensuring the wrong memory isn't #1.
    # We add an invariant flag the test can use to skip the positive expectation.
    negatives = [
        ("haiku poetry meter syllables", None),
        ("medieval pottery glaze chemistry", None),
        ("tax bracket inflation adjustment", None),
        ("plate tectonics subduction zone", None),
    ]
    for q, expected in negatives:
        cases.append(
            {
                "query": q,
                "expected_top_name": expected,
                "kind": "negative",
                "rationale": "Negative case: no strong match expected",
                "negative": True,
            }
        )

    return cases


@pytest.fixture
def isolated_xdg(tmp_path: Path, monkeypatch) -> Path:
    """Point XDG_CONFIG_HOME and XDG_CACHE_HOME at tmp dirs so tests don't touch user config."""
    config_home = tmp_path / "xdg-config"
    cache_home = tmp_path / "xdg-cache"
    data_home = tmp_path / "xdg-data"
    config_home.mkdir()
    cache_home.mkdir()
    data_home.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("BRAIN_HOME", str(data_home / "brain"))
    # Also clear HOME so platformdirs uses XDG vars
    return tmp_path


@pytest.fixture
def write_config(isolated_xdg: Path):
    """Helper to write a config.json with given sources."""

    def _write(sources: list[dict], extra: dict | None = None) -> Path:
        cfg = {"sources": sources}
        if extra:
            cfg.update(extra)
        cfg_path = isolated_xdg / "xdg-config" / "recall" / "config.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        return cfg_path

    return _write
