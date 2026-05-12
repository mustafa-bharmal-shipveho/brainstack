"""Topic-key + predicate extraction framework.

Producer events are opaque JSONL dicts. This module turns one event into
zero or more `Claim` tuples that the consolidator (PR3) feeds into the
event-sourced claim log.

Framework rules (HARD; enforced by tests):
  - No producer-specific code paths. NEVER inspect `event["source"]`.
    Adding a new producer is a config change, not a code change.
  - Topic-key derivation uses only the documented event fields. Optional
    fields (counterparty, channel_id) are consulted opportunistically.
  - Predicate library is data-driven. Adding a predicate is a TOML edit.

Two modes for topic-key extraction:

  permissive   (default when `projects.toml` is empty or absent)
               Any uppercase token ≥ 3 chars that's not in the stoplist
               becomes a `project:<token>` key. Trade-off: discovers
               unknown projects without operator input.

  allowlist    (when `projects.toml` has entries)
               Only tokens that match a `[projects]` entry (or one of
               its aliases) become `project:<canonical>`. Trade-off:
               operator-tight; nothing else slips through.

Predicate library (built-in v1):
  release-date  matches release/launch verbs; normalizes to ISO date.
                Relative dates ("Monday", "next week") produce no claim.
  status        matches status phrases; normalizes to a small enum.
  deadline      matches deadline phrases; normalizes to ISO date.
  owner         matches owner phrases; normalizes via the `person` stub
                (no person map in v1 — token is lowercased and returned
                as-is, marked as a documented limitation).
  decision      matches decision phrases; normalizes via `freeform-2k`.

Match rules (apply to every predicate):
  - Case-insensitive word-boundary match against the predicate's
    `match` list. Substring matches are forbidden (`launchpad` does
    not match `launch`).
  - Negation skip: if any of {"not", "won't", "isn't", ...} appears
    within 3 tokens BEFORE the match, the event produces no claim
    for that predicate.
  - If the normalizer returns None for the window of text around the
    match, no claim is produced.

The default config (`DEFAULT_PREDICATES` below) ships hardcoded so
brainstack works out-of-the-box. Operators override by writing
`~/.config/brainstack/extractors.toml` (loaded by `load_config`).
"""
from __future__ import annotations

import datetime
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Tuple

try:
    import regex as _regex_module  # type: ignore
    _HAVE_REGEX = True
except ImportError:  # pragma: no cover
    _regex_module = None
    _HAVE_REGEX = False

# Python 3.11+ ships tomllib in stdlib. Earlier Pythons need `tomli` or
# fall back to a no-config posture.
try:
    import tomllib  # type: ignore[attr-defined]
    _HAVE_TOML = True
except ImportError:
    try:
        import tomli as tomllib  # type: ignore
        _HAVE_TOML = True
    except ImportError:  # pragma: no cover
        tomllib = None
        _HAVE_TOML = False


# --- Claim record (what an extractor emits) ---------------------------

@dataclass(frozen=True)
class Claim:
    """One claim derived from one (event, predicate) pair.

    Multiple `Claim`s can come from a single event: one event with two
    topic keys × two matching predicates yields four claims.
    """
    topic_key: str
    claim_subject: str
    value_normalized: str
    value_raw: str


class TopicKeyExtractor(Protocol):
    """Pluggable extractor interface. Implementations MUST be pure
    (no I/O, no `now()` calls) so consolidation is idempotent."""

    def extract(self, event: Dict[str, Any]) -> List[Claim]:
        ...


# --- Stoplist (used in permissive mode) -------------------------------

# Common English uppercase tokens that are NOT projects. Operators can
# override with `~/.config/brainstack/stoplist.toml` (`[stoplist] words`).
DEFAULT_STOPLIST: Tuple[str, ...] = (
    "AND", "THE", "ABC", "FYI", "ASAP", "ETC", "USA", "UK", "EU", "OK",
    "OKAY", "NOW", "NEW", "EOD", "EOW", "BTW", "TLDR", "NYC", "ATL",
    "OOO", "WIP", "TBD", "FAQ", "AKA", "ALL", "URL", "API", "CSV",
    "SQL", "AWS", "GCP", "PR", "PRD", "DOC", "DOCS", "TODO", "DONE",
    "ETA", "Q1", "Q2", "Q3", "Q4", "IS", "ARE", "WAS", "WERE",
)


# --- Predicate library (v1 defaults) ---------------------------------

# Phrases per predicate. The match list is consulted case-insensitively
# with word boundaries. v1 defaults err on the side of HIGH PRECISION:
# better to miss a soft signal than to manufacture noise from casual
# chat. Operators tighten or widen via `~/.config/brainstack/extractors.toml`.
DEFAULT_PREDICATES: Dict[str, Dict[str, Any]] = {
    "release-date": {
        "match": ["launches", "launching", "ships on", "shipping on",
                  "releases on", "releasing on", "go-live", "go live",
                  "ga on", "rolls out on", "rolling out on", "launch on"],
        "normalizer": "date",
    },
    "status": {
        # The bare words "done", "shipped", "blocked" appear constantly
        # in casual chat ("get it done", "the issue was shipped"). Only
        # match explicit status-report shapes:
        "match": ["status:", "status is", "the status is",
                  "currently blocked", "currently in progress",
                  "now blocked"],
        "normalizer": "enum",
        "enum_values": ["blocked", "in-progress", "done", "shipped",
                        "stuck", "unknown"],
    },
    "deadline": {
        "match": ["deadline:", "deadline is", "due on", "due by"],
        "normalizer": "date",
    },
    "owner": {
        "match": ["owner:", "owner is", "assignee:", "assignee is", "dri:"],
        "normalizer": "person",
    },
    "decision": {
        "match": ["we decided", "decision:", "going with"],
        "normalizer": "freeform-2k",
    },
}


# Words that negate a predicate match if they appear within
# NEGATION_LOOKBEHIND_TOKENS tokens BEFORE the match.
NEGATION_TOKENS: Tuple[str, ...] = (
    "not", "won't", "wont", "isn't", "isnt", "wasn't", "wasnt",
    "no longer", "wouldn't", "wouldnt", "doesn't", "doesnt",
    "didn't", "didnt", "shouldn't", "shouldnt", "haven't", "havent",
    "never",
)
NEGATION_LOOKBEHIND_TOKENS = 3


# --- Normalizers ------------------------------------------------------

# Strict ISO 8601: YYYY-MM-DD, optionally with time + TZ. Plan says
# relative dates ("Monday", "by EOW") produce NO claim — so we accept
# only fully-qualified dates by design.
_ISO_DATE_RE = re.compile(
    r"\b(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?(Z|[+-]\d{2}:?\d{2})?)?\b"
)


def _normalize_date(window: str) -> Optional[str]:
    """Find the first ISO 8601 date in `window`; return YYYY-MM-DD.

    Relative dates intentionally produce None (out of scope for v1).
    """
    m = _ISO_DATE_RE.search(window)
    if not m:
        return None
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        d = datetime.date(year, month, day)
    except ValueError:
        return None
    return d.isoformat()


def _normalize_enum(window: str, enum_values: List[str]) -> Optional[str]:
    """Return the canonical enum value matching `window`, or 'unknown'.

    The enum list is consulted in declared order; first hit wins.
    """
    text = window.lower()
    for v in enum_values:
        if re.search(rf"\b{re.escape(v.lower())}\b", text):
            return v
    return "unknown" if "unknown" in [v.lower() for v in enum_values] else None


def _normalize_person(window: str) -> Optional[str]:
    """v1 stub: lowercase the first capitalized token (or @mention) in
    `window`. The caller is responsible for slicing the post-match
    region so we don't accidentally grab a project token that appears
    BEFORE the role label (e.g. "PS2 owner Alice" → caller passes
    " Alice", not "PS2 owner Alice"; we'd otherwise return "ps2").

    Canonical person resolution (U-id ↔ display name) is a PR6
    follow-up — see Accepted v1 limitations in the plan.
    """
    m = re.search(r"@?([A-Z][a-zA-Z0-9._-]+)", window)
    if m:
        return m.group(1).lower()
    return None


def _normalize_freeform_2k(window: str) -> Optional[str]:
    """Lowercase + strip + cap at 2KB. Returns None for empty windows."""
    text = (window or "").strip().lower()
    if not text:
        return None
    return text[:2000]


# Dispatch table (data, not branching on event content).
_NORMALIZERS: Dict[str, Callable[..., Optional[str]]] = {
    "date": lambda window, **_: _normalize_date(window),
    "enum": lambda window, enum_values=None, **_:
        _normalize_enum(window, enum_values or []),
    "person": lambda window, **_: _normalize_person(window),
    "freeform-2k": lambda window, **_: _normalize_freeform_2k(window),
}


# --- Config loading ---------------------------------------------------

@dataclass(frozen=True)
class ExtractorConfig:
    projects: Dict[str, List[str]] = field(default_factory=dict)  # canonical → aliases
    stoplist: Tuple[str, ...] = DEFAULT_STOPLIST
    channels: Dict[str, str] = field(default_factory=dict)  # channel_id → name
    predicates: Dict[str, Dict[str, Any]] = field(
        default_factory=lambda: dict(DEFAULT_PREDICATES))


def _config_dir() -> str:
    """Operator config root. Honors $XDG_CONFIG_HOME like other tools."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return os.path.join(xdg, "brainstack")
    return os.path.expanduser("~/.config/brainstack")


class ExtractorConfigError(ValueError):
    """Raised when an operator config file is malformed.

    Missing files are tolerated (defaults win), but a typo'd TOML must
    surface — otherwise a single character in `projects.toml` can
    silently flip allowlist mode → permissive mode and start emitting
    unintended `project:<token>` keys.
    """


def _load_toml(path: str) -> Dict[str, Any]:
    if not _HAVE_TOML or not os.path.exists(path):
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except OSError:
        return {}
    except Exception as exc:
        # tomllib.TOMLDecodeError subclasses ValueError; we surface it as
        # ExtractorConfigError so operators see typos in their config.
        raise ExtractorConfigError(
            f"malformed brainstack config at {path}: {exc}"
        ) from exc


def load_config(config_dir: Optional[str] = None) -> ExtractorConfig:
    """Read operator config from `~/.config/brainstack/`.

    Layout:
      extractors.toml    [predicates.<name>] match=[..], normalizer="..",
                         [conflict_thresholds] (PR3 only)
      projects.toml      [projects] PS2 = ["ps2", "playstation-2"]
                         (or PS2 = { aliases = [...] })
      channels.toml      [channels] C0X = "network-leadership"
      stoplist.toml      [stoplist] words = ["AND", "THE", ...]

    Missing files / fields are tolerated; defaults win.
    """
    d = config_dir or _config_dir()

    projects_raw = _load_toml(os.path.join(d, "projects.toml")).get("projects", {})
    projects: Dict[str, List[str]] = {}
    for canonical, val in projects_raw.items():
        if isinstance(val, list):
            projects[canonical] = [str(x).lower() for x in val]
        elif isinstance(val, dict):
            projects[canonical] = [str(x).lower() for x in val.get("aliases", [])]

    channels_raw = _load_toml(os.path.join(d, "channels.toml")).get("channels", {})
    channels: Dict[str, str] = {str(k): str(v) for k, v in channels_raw.items()}

    stoplist_raw = _load_toml(os.path.join(d, "stoplist.toml")).get("stoplist", {})
    if isinstance(stoplist_raw, dict) and "words" in stoplist_raw:
        stoplist: Tuple[str, ...] = tuple(str(w).upper() for w in stoplist_raw["words"])
    else:
        stoplist = DEFAULT_STOPLIST

    extractors_raw = _load_toml(os.path.join(d, "extractors.toml"))
    predicates_cfg = extractors_raw.get("predicates", {})
    predicates: Dict[str, Dict[str, Any]] = dict(DEFAULT_PREDICATES)
    if isinstance(predicates_cfg, dict):
        # Operators can override or add predicates.
        for name, body in predicates_cfg.items():
            if not isinstance(body, dict):
                continue
            predicates[name] = dict(body)

    return ExtractorConfig(
        projects=projects,
        stoplist=stoplist,
        channels=channels,
        predicates=predicates,
    )


# --- Topic-key derivation --------------------------------------------

_UPPER_TOKEN_FALLBACK_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,}\b")

def _is_project_token_shape(tok: str) -> bool:
    """Permissive-mode project tokens must be letters with optional
    trailing digits — e.g. PS2, OKR, MYPROJ, MÜLLER. Slack channel/user
    IDs like D0A8DQ7BP0U or U05LZF28SRH have digits interleaved with
    letters and would otherwise pollute the topic-key space.

    Unicode-aware via stdlib `str.isalpha` / `str.isdigit` — works on
    MÜLLER and similar without needing the `regex` package.
    """
    if not tok:
        return False
    i = 0
    while i < len(tok) and not tok[i].isdigit():
        i += 1
    letters_part, digits_part = tok[:i], tok[i:]
    if not letters_part:
        return False
    if not all(c.isalpha() and c.isupper() for c in letters_part):
        return False
    if not all(c.isdigit() for c in digits_part):
        return False
    return True


def _tokenize_upper(body: str) -> List[str]:
    """Find uppercase tokens. Unicode-aware via `regex` if available;
    falls back to ASCII via stdlib `re`."""
    if _HAVE_REGEX:
        # \p{Lu} = uppercase letter, \p{Nd} = decimal digit
        try:
            tokens = _regex_module.findall(  # type: ignore[attr-defined]
                r"\b[\p{Lu}][\p{Lu}\p{Nd}]+\b", body
            )
            return list(tokens)
        except Exception:  # pragma: no cover
            pass
    return _UPPER_TOKEN_FALLBACK_RE.findall(body)


def _topic_keys_from_body(body: str, config: ExtractorConfig) -> List[str]:
    """Derive `project:*` topic keys from body text per the configured mode."""
    tokens = _tokenize_upper(body)
    keys: List[str] = []
    seen: set = set()

    if config.projects:
        # Allowlist mode: only canonical projects (or their aliases).
        alias_lookup: Dict[str, str] = {}
        for canonical, aliases in config.projects.items():
            alias_lookup[canonical.lower()] = canonical
            for a in aliases:
                alias_lookup[a.lower()] = canonical
        # Also try the full body for multi-word aliases (e.g. "playstation 2").
        body_lower = body.lower()
        for canonical, aliases in config.projects.items():
            for phrase in [canonical] + aliases:
                if re.search(rf"\b{re.escape(phrase.lower())}\b", body_lower):
                    key = f"project:{canonical.lower()}"
                    if key not in seen:
                        seen.add(key)
                        keys.append(key)
                    break
    else:
        # Permissive mode: uppercase tokens, non-stoplisted, and shaped
        # like a project code (letters with optional trailing digits).
        # Slack channel IDs (D0A8DQ7BP0U) and user IDs (U05LZF28SRH)
        # have digits interleaved, so they fail the shape check and
        # don't pollute the topic-key space.
        stop_upper = {s.upper() for s in config.stoplist}
        for tok in tokens:
            tok_u = tok.upper()
            if len(tok_u) < 3:
                continue
            if tok_u in stop_upper:
                continue
            if not _is_project_token_shape(tok_u):
                continue
            key = f"project:{tok_u.lower()}"
            if key not in seen:
                seen.add(key)
                keys.append(key)
    return keys


def _opportunistic_keys(event: Dict[str, Any],
                       config: ExtractorConfig) -> List[str]:
    """Best-effort keys from optional fields. Absence is never an error.
    The event's `source` field is NEVER consulted (framework rule)."""
    keys: List[str] = []
    cp = event.get("counterparty")
    if cp:
        keys.append(f"person:{str(cp).lower()}")
    ch = event.get("channel_id")
    if ch:
        # Use a channel name if configured, otherwise the raw id.
        name = config.channels.get(str(ch))
        if name and event.get("channel_type") == "channel":
            keys.append(f"team:{name.lower()}")
        else:
            keys.append(f"channel:{str(ch).lower()}")
    return keys


# --- Predicate matching ----------------------------------------------

_WORD_CHAR_RE = re.compile(r"\w")


def _boundary_regex(phrase: str) -> str:
    """Build a regex that enforces word-boundary semantics only at the
    word-char endpoints of `phrase`.

    For a phrase like ``launch``: `\\blaunch\\b` (both ends are word
    chars, so use `\\b`).

    For a phrase ending with a non-word char like ``decision:``: enforce
    the boundary only on the leading word char. The trailing `:` is
    already a hard token separator; requiring `\\b` after it incorrectly
    fails on common cases like ``decision: foo`` (since `\\b` after `:`
    only matches when the next char is a word char).
    """
    left = r"\b" if phrase and _WORD_CHAR_RE.match(phrase[0]) else ""
    right = r"\b" if phrase and _WORD_CHAR_RE.match(phrase[-1]) else ""
    return left + re.escape(phrase) + right


def _find_predicate_matches(body: str, predicate: Dict[str, Any]
                            ) -> List[Tuple[int, int, str]]:
    """Return list of (start, end, matched_phrase) for every word-boundary,
    case-insensitive match of any phrase in `predicate['match']`.

    Boundary semantics adapt to phrase endpoints — phrases ending with
    `:`, `?`, etc. don't require a `\\b` on the non-word side (which
    would fail unpredictably in practice)."""
    matches: List[Tuple[int, int, str]] = []
    for phrase in predicate.get("match", []) or []:
        if not phrase:
            continue
        pattern = _boundary_regex(phrase)
        for m in re.finditer(pattern, body, flags=re.IGNORECASE):
            matches.append((m.start(), m.end(), phrase))
    # Sort by position so window extraction is deterministic.
    matches.sort()
    return matches


def _is_negated(body: str, match_start: int) -> bool:
    """True if a negation token appears within NEGATION_LOOKBEHIND_TOKENS
    tokens BEFORE `match_start`."""
    preceding = body[:match_start]
    # Take the last N tokens of preceding text.
    tail = re.findall(r"\S+", preceding)[-NEGATION_LOOKBEHIND_TOKENS:]
    tail_lower = " ".join(tail).lower()
    for neg in NEGATION_TOKENS:
        if re.search(rf"\b{re.escape(neg)}\b", tail_lower):
            return True
    return False


def _window_around(body: str, start: int, end: int,
                  tokens_each_side: int = 20) -> str:
    """Take a ±N-token window around the match (inclusive)."""
    pre = body[:start]
    post = body[end:]
    pre_tokens = re.findall(r"\S+", pre)[-tokens_each_side:]
    post_tokens = re.findall(r"\S+", post)[:tokens_each_side]
    parts: List[str] = []
    if pre_tokens:
        parts.append(" ".join(pre_tokens))
    parts.append(body[start:end])
    if post_tokens:
        parts.append(" ".join(post_tokens))
    return " ".join(parts)


# --- The Heuristic extractor -----------------------------------------

class HeuristicExtractor:
    """Default v1 extractor. Pure (no I/O, no `now()` once instantiated).

    Constructor accepts an explicit `ExtractorConfig` to keep extraction
    deterministic and test-isolated. Passing `config=None` does NOT
    read disk anymore — callers wanting operator config must call
    `load_config()` themselves and pass the result. Disk reads at
    construction time were a footgun: tests couldn't tell whether two
    `HeuristicExtractor()` calls were using the same effective config
    [codex PR2 P2 fix].
    """

    def __init__(self, config: Optional[ExtractorConfig] = None):
        self.config = config or ExtractorConfig()

    def extract(self, event: Dict[str, Any]) -> List[Claim]:
        body = event.get("body_redacted") or ""
        if not isinstance(body, str) or not body.strip():
            return []

        topic_keys = _topic_keys_from_body(body, self.config)
        opp = _opportunistic_keys(event, self.config)
        all_keys = topic_keys + opp
        if not all_keys:
            return []

        # Storage invariant (PR1 claims.py): claim_id =
        # sha256(topic_key + claim_subject + source_event_id). One event
        # MUST emit at most one claim per (topic_key, claim_subject)
        # slot — otherwise a second assert silently overwrites the
        # first because both share the same claim_id. First-match wins
        # per slot [codex PR2 P1 fix].
        seen_slots: set = set()
        out: List[Claim] = []
        for pred_name, pred in self.config.predicates.items():
            matches = _find_predicate_matches(body, pred)
            for start, end, _phrase in matches:
                if _is_negated(body, start):
                    continue
                window = _window_around(body, start, end)
                norm_name = pred.get("normalizer")
                norm_fn = _NORMALIZERS.get(norm_name)
                if norm_fn is None:
                    continue
                kwargs: Dict[str, Any] = {}
                if norm_name == "enum":
                    kwargs["enum_values"] = pred.get("enum_values", [])
                # `person` reads only the tokens AFTER the match (the
                # name follows the role label like "owner Alice"),
                # so it gets a tighter slice than the full ±20 window.
                if norm_name == "person":
                    post_only = body[end:end + 200]
                    value_normalized = norm_fn(post_only)
                else:
                    value_normalized = norm_fn(window, **kwargs)
                if value_normalized is None:
                    continue
                for topic in all_keys:
                    slot = (topic, pred_name)
                    if slot in seen_slots:
                        continue
                    seen_slots.add(slot)
                    out.append(Claim(
                        topic_key=topic,
                        claim_subject=pred_name,
                        value_normalized=value_normalized,
                        value_raw=window,
                    ))
        return out


# --- Registry --------------------------------------------------------

def default_extractors(config_dir: Optional[str] = None) -> List[TopicKeyExtractor]:
    """The default v1 extractor registry.

    Reads operator config from `config_dir` (default
    `~/.config/brainstack`) via `load_config()` — disk I/O is here, NOT
    in `HeuristicExtractor.__init__`. PR3+ may register additional
    extractors. Stub-registered alternates raise on use so framework-
    shape compliance is forced from day one.
    """
    return [HeuristicExtractor(load_config(config_dir))]
