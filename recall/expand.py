"""Query expansion via LLM.

For a user query, generate N paraphrases that share intent but use different
vocabulary. The retriever runs each variant separately and a fusion step
merges the results — this dramatically improves Recall@10 on queries whose
phrasing doesn't share vocab with the target documents.

Implementation uses the existing brainstack LLM provider registry
(`agent.tools.llm_providers`) so it inherits provider auto-detection,
config-driven model selection, and budget/timeout controls. No new
dependency.

Empirical result on a 38-query LLM-paraphrased hard set against a real
brain (744 docs):

    Config                                  Recall@10  NDCG@10  p50
    -----------------------------------     ---------  -------  ------
    baseline (no expand, no rerank)         44.7%      26.0%    24ms
    +4-var expansion (RRF merge)            68.4%      36.8%    130ms
    +4-var expansion + bge post-rerank      63.2%      42.1%    2476ms

The expansion call itself is the latency bottleneck (one LLM round-trip).
Cache by (query, n) so a stable session paying that cost once amortizes.
"""
from __future__ import annotations

import functools
import json
from typing import Optional


# System prompt is deliberately concrete: the failure mode of vague LLMs
# is generating paraphrases that just rephrase syntax without changing
# vocabulary (still high lexical overlap with original). The examples
# show what a good vocabulary-shift looks like.
_SYSTEM = (
    "You are a query-expansion helper for a memory-retrieval system. "
    "Given a user query, write N paraphrases that share INTENT but use "
    "DIFFERENT WORDS. The retrieval system uses lexical AND semantic "
    "matching; your paraphrases should help it find docs whose authors "
    "phrased the same idea differently from the user.\n\n"
    "GOOD paraphrase: original=\"how do I plan a quarter of engineering work\", "
    "paraphrase=\"capacity planning + roadmap setup for a 3-month cycle\".\n"
    "BAD paraphrase: original=\"how do I plan a quarter of engineering work\", "
    "paraphrase=\"how does someone plan one quarter of engineering work\" "
    "(too similar; same vocabulary).\n\n"
    "Mix abstraction levels: one technical (use synonyms), one descriptive "
    "(reframe the situation), one terse (3-5 words). Return JSON only."
)


@functools.lru_cache(maxsize=512)
def _cached_expand(query: str, n: int, provider_name: Optional[str]) -> tuple[str, ...]:
    """Cached implementation; returns tuple so it's hashable + immutable."""
    # Local import — keeps top-level import cheap and avoids cycles.
    from agent.tools.llm_providers import resolve_provider

    provider = resolve_provider(provider_name)

    prompt = (
        f"Original query: {query!r}\n\n"
        f"Return EXACTLY {n} paraphrases as JSON:\n"
        '{"paraphrases": ["...", "...", "..."]}'
    )

    schema = {
        "type": "object",
        "required": ["paraphrases"],
        "properties": {
            "paraphrases": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": n,
                "maxItems": n,
            }
        },
    }

    result = provider.invoke(
        system=_SYSTEM,
        prompt=prompt,
        json_schema=schema,
        max_budget_usd=0.05,
        timeout_s=20,
    )
    try:
        data = json.loads(result.text)
        paraphrases = data["paraphrases"]
    except (json.JSONDecodeError, KeyError, TypeError):
        # Provider's retry-once contract should have caught this. If we're
        # here something genuinely broke; fail open (return original only).
        return (query,)

    # Always include the original as variant #0 (priority preserved by RRF).
    # Dedupe paraphrases against the original AND against each other —
    # an LLM that returns ["foo", "foo", "bar"] would otherwise make the
    # retriever run "foo" twice for nothing.
    out = [query]
    seen = {query.strip().lower()}
    for p in paraphrases:
        if not isinstance(p, str):
            continue
        normalized = p.strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(normalized)
    return tuple(out)


def expand_query(
    query: str,
    n: int = 3,
    provider: Optional[str] = None,
) -> list[str]:
    """Return [original_query, paraphrase_1, ..., paraphrase_n].

    `provider` follows the brainstack LLM-provider precedence (env var
    BRAIN_LLM_PROVIDER, TOML config, auto-detect). Pass an explicit name to
    pin a specific provider for the call.

    The expansion is cached per (query, n, provider) within the process so
    repeated queries don't pay the LLM round-trip twice. Cache size is 512
    entries; LRU eviction beyond that. Call `_cached_expand.cache_clear()`
    to reset (useful for benchmarks or tests).

    On any provider failure, returns [query] only — the rest of the
    retrieval pipeline still works without expansion.
    """
    if n <= 0:
        return [query]
    try:
        return list(_cached_expand(query, n, provider))
    except Exception:
        # Fail-open: any LLM provider issue (timeout, auth, network) falls
        # back to the original query alone. Callers see a degraded but
        # functional retrieval, not a hard error.
        return [query]
