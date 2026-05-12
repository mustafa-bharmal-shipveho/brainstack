"""Tests for topic_keys.py — the producer-agnostic extraction framework.

Coverage:
  - Topic-key derivation in both permissive and allowlist modes.
  - Stoplist suppression in permissive mode.
  - Opportunistic keys from optional fields (counterparty, channel_id).
  - Word-boundary + case-insensitive predicate matching.
  - Negation skip (event produces 0 claims when match is negated).
  - Each normalizer (date ISO-only, enum, person stub, freeform-2k).
  - Multi-predicate × multi-topic-key cardinality.
  - No-predicate-match → empty claim list.
  - Pure semantics: same input → same output (no `now()` calls).
  - Framework shape: no producer name branching in the module.
  - End-to-end on synthetic NON-AGENTRY producers.
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent" / "memory"))

import topic_keys  # noqa: E402


# --- Topic-key derivation ----------------------------------------------

def test_permissive_mode_picks_uppercase_tokens_not_in_stoplist():
    cfg = topic_keys.ExtractorConfig()  # empty projects → permissive
    body = "PS2 launches 2026-05-18 according to the release plan"
    keys = topic_keys._topic_keys_from_body(body, cfg)
    assert "project:ps2" in keys
    # "THE" is stoplisted; ensure no project:the.
    assert "project:the" not in keys


def test_permissive_mode_stoplist_default_blocks_common_false_positives():
    cfg = topic_keys.ExtractorConfig()
    body = "AND THE ABC FYI USA - PS2 launches 2026-05-18"
    keys = topic_keys._topic_keys_from_body(body, cfg)
    for blocked in ("project:and", "project:the", "project:abc",
                    "project:fyi", "project:usa"):
        assert blocked not in keys
    assert "project:ps2" in keys


def test_permissive_mode_skips_tokens_under_3_chars():
    cfg = topic_keys.ExtractorConfig()
    body = "AI XR Q ships 2026-05-18"
    keys = topic_keys._topic_keys_from_body(body, cfg)
    # AI is 2 chars → skipped. XR is 2 → skipped. Q is 1 → skipped.
    assert "project:ai" not in keys
    assert "project:xr" not in keys
    assert "project:q" not in keys


def test_allowlist_mode_requires_project_in_map():
    cfg = topic_keys.ExtractorConfig(projects={"PS2": ["ps2", "playstation-2"]})
    # PS2 in body → matches. RANDOM in body → ignored.
    body = "PS2 launches on 2026-05-18; also RANDOM is launching on 2026-05-19"
    keys = topic_keys._topic_keys_from_body(body, cfg)
    assert "project:ps2" in keys
    assert "project:random" not in keys


def test_allowlist_mode_matches_aliases():
    cfg = topic_keys.ExtractorConfig(projects={"PS2": ["playstation-2"]})
    body = "the playstation-2 launches on 2026-05-18"
    keys = topic_keys._topic_keys_from_body(body, cfg)
    assert "project:ps2" in keys


def test_unicode_uppercase_tokens_recognized_when_regex_available():
    # Only meaningful if the `regex` library is installed (default on 3.13 venv).
    if not topic_keys._HAVE_REGEX:
        pytest.skip("regex package not installed")
    cfg = topic_keys.ExtractorConfig()
    body = "MÜLLER project launches on 2026-05-18"
    keys = topic_keys._topic_keys_from_body(body, cfg)
    assert any("müller" in k.lower() or "project:" in k for k in keys)


def test_opportunistic_counterparty_becomes_person_key():
    cfg = topic_keys.ExtractorConfig()
    event = {"body_redacted": "no project tokens here",
             "counterparty": "U123ABC"}
    keys = topic_keys._opportunistic_keys(event, cfg)
    assert "person:u123abc" in keys


def test_opportunistic_channel_named_becomes_team_key():
    cfg = topic_keys.ExtractorConfig(channels={"C0X": "network-leadership"})
    event = {"body_redacted": "x", "channel_id": "C0X",
             "channel_type": "channel"}
    keys = topic_keys._opportunistic_keys(event, cfg)
    assert "team:network-leadership" in keys


def test_opportunistic_channel_unnamed_becomes_channel_key():
    cfg = topic_keys.ExtractorConfig()
    event = {"body_redacted": "x", "channel_id": "C0X",
             "channel_type": "channel"}
    keys = topic_keys._opportunistic_keys(event, cfg)
    assert "channel:c0x" in keys


# --- Predicate matching ----------------------------------------------

def test_word_boundary_no_substring_match():
    body = "the launchpad is great"
    matches = topic_keys._find_predicate_matches(
        body, topic_keys.DEFAULT_PREDICATES["release-date"],
    )
    # "launch" inside "launchpad" must NOT match.
    assert not any(m[2] == "launch" for m in matches)


def test_case_insensitive_match():
    body = "PS2 LAUNCHES on 2026-05-18"
    matches = topic_keys._find_predicate_matches(
        body, topic_keys.DEFAULT_PREDICATES["release-date"],
    )
    assert any(m[2].lower() == "launches" for m in matches)


def test_negation_skip_with_not():
    body = "PS2 is not launching on 2026-05-18"
    # Find the "launching" match. _is_negated should return True.
    matches = topic_keys._find_predicate_matches(
        body, topic_keys.DEFAULT_PREDICATES["release-date"],
    )
    assert matches  # "launching" is found
    start = matches[0][0]
    assert topic_keys._is_negated(body, start)


def test_negation_skip_with_wont():
    body = "we won't launch on 2026-05-18"
    matches = topic_keys._find_predicate_matches(
        body, topic_keys.DEFAULT_PREDICATES["release-date"],
    )
    assert matches
    assert topic_keys._is_negated(body, matches[0][0])


def test_no_negation_when_word_too_far_back():
    # Negation appears > 3 tokens before "launches" → no skip.
    body = "we are not really sure if PS2 launches on 2026-05-18"
    matches = topic_keys._find_predicate_matches(
        body, topic_keys.DEFAULT_PREDICATES["release-date"],
    )
    assert matches
    assert not topic_keys._is_negated(body, matches[0][0])


# --- Normalizers ------------------------------------------------------

def test_date_normalizer_accepts_iso_8601():
    assert topic_keys._normalize_date("launches on 2026-05-18") == "2026-05-18"
    assert topic_keys._normalize_date("launches on 2026-05-18T10:30:00Z") == "2026-05-18"


def test_date_normalizer_rejects_relative_dates():
    # Plan accepted limitation: relative dates produce no claim.
    assert topic_keys._normalize_date("launches on Monday") is None
    assert topic_keys._normalize_date("by EOW") is None
    assert topic_keys._normalize_date("next week") is None


def test_date_normalizer_rejects_invalid_dates():
    # Feb 30 is not a valid date.
    assert topic_keys._normalize_date("ships on 2026-02-30") is None


def test_enum_normalizer_picks_canonical():
    enum = ["blocked", "in-progress", "done", "shipped", "unknown"]
    assert topic_keys._normalize_enum("the status is blocked", enum) == "blocked"
    assert topic_keys._normalize_enum("we are in-progress", enum) == "in-progress"


def test_enum_normalizer_unknown_for_nonmatching():
    enum = ["blocked", "in-progress", "done", "unknown"]
    assert topic_keys._normalize_enum("no idea", enum) == "unknown"


def test_person_normalizer_stub_lowercases_first_capital():
    # v1 limitation: no person map; lowercase the first capitalized token.
    # Caller is expected to pass POST-match text only.
    assert topic_keys._normalize_person(" Alice") == "alice"
    assert topic_keys._normalize_person(" @Bob") == "bob"


def test_extractor_owner_does_not_capture_project_token():
    """Codex PR2 P2 fix: owner normalizer ran over the full window, so
    "PS2 owner: Alice" returned 'ps2'. Now we slice to post-match
    only, so we get 'alice'."""
    ex = topic_keys.HeuristicExtractor(topic_keys.ExtractorConfig())
    out = ex.extract({
        "body_redacted": "PS2 owner: Alice",
        "source": "research-notes",
        "event_id": "n:1",
    })
    owners = [c for c in out
              if c.topic_key == "project:ps2" and c.claim_subject == "owner"]
    assert len(owners) == 1
    assert owners[0].value_normalized == "alice"


def test_freeform_2k_normalizer_strips_and_lowercases():
    out = topic_keys._normalize_freeform_2k("  WE Decided to Ship  ")
    assert out == "we decided to ship"


def test_freeform_2k_caps_at_2kb():
    long = "x" * 5000
    out = topic_keys._normalize_freeform_2k(long)
    assert len(out) == 2000


def test_freeform_2k_returns_none_for_empty():
    assert topic_keys._normalize_freeform_2k("") is None
    assert topic_keys._normalize_freeform_2k("   ") is None


# --- HeuristicExtractor end-to-end -----------------------------------

def test_extractor_empty_body_returns_empty():
    ex = topic_keys.HeuristicExtractor(topic_keys.ExtractorConfig())
    assert ex.extract({"body_redacted": ""}) == []
    assert ex.extract({}) == []


def test_extractor_no_topic_keys_returns_empty():
    """An event with no project tokens and no optional fields → 0 claims."""
    ex = topic_keys.HeuristicExtractor(topic_keys.ExtractorConfig())
    out = ex.extract({"body_redacted": "lowercase only no projects",
                      "source": "research-notes"})
    assert out == []


def test_extractor_emits_release_date_claim():
    ex = topic_keys.HeuristicExtractor(topic_keys.ExtractorConfig())
    out = ex.extract({
        "body_redacted": "PS2 launches on 2026-05-18",
        "source": "research-notes",
        "event_id": "n:1",
        "source_ts": "1700000000.0",
    })
    assert len(out) == 1
    assert out[0].topic_key == "project:ps2"
    assert out[0].claim_subject == "release-date"
    assert out[0].value_normalized == "2026-05-18"


def test_extractor_negated_match_produces_no_claim():
    ex = topic_keys.HeuristicExtractor(topic_keys.ExtractorConfig())
    out = ex.extract({
        "body_redacted": "PS2 is not launching on 2026-05-18",
        "source": "research-notes",
    })
    assert out == []


def test_extractor_relative_date_produces_no_claim():
    ex = topic_keys.HeuristicExtractor(topic_keys.ExtractorConfig())
    out = ex.extract({
        "body_redacted": "PS2 launches Monday",
        "source": "research-notes",
    })
    assert out == []


def test_extractor_multi_topic_multi_predicate_yields_cardinality():
    """One event with two project tokens × two matching predicates →
    four distinct claims (modulo dedupe on identical topic×subj×value)."""
    ex = topic_keys.HeuristicExtractor(topic_keys.ExtractorConfig())
    out = ex.extract({
        "body_redacted": "PS2 and OKR are launching on 2026-05-18 and 2026-05-19",
        "source": "research-notes",
    })
    # Two topics × release-date predicate × (at most two dates)
    # = up to 4 claims. Each is unique by (topic, subject, value).
    assert len(out) >= 2
    topics = {c.topic_key for c in out}
    assert "project:ps2" in topics
    assert "project:okr" in topics
    for c in out:
        assert c.claim_subject == "release-date"


def test_extractor_dedupes_identical_claims():
    """If predicate matches twice with the same normalized date, only
    one claim is emitted per topic."""
    ex = topic_keys.HeuristicExtractor(topic_keys.ExtractorConfig())
    out = ex.extract({
        "body_redacted": "PS2 launches on 2026-05-18, ships on 2026-05-18",
        "source": "research-notes",
    })
    ps2_release = [c for c in out
                   if c.topic_key == "project:ps2"
                   and c.claim_subject == "release-date"]
    assert len(ps2_release) == 1


def test_extractor_is_pure_no_now_calls():
    """Two extracts of the SAME event must yield identical claims —
    no `now()` smuggled into the output."""
    ex = topic_keys.HeuristicExtractor(topic_keys.ExtractorConfig())
    event = {"body_redacted": "PS2 launches on 2026-05-18",
             "source": "research-notes"}
    a = ex.extract(event)
    b = ex.extract(event)
    assert a == b


def test_extractor_works_with_synthetic_unknown_producer():
    """The framework MUST work on a producer it has never seen. No code
    change required."""
    ex = topic_keys.HeuristicExtractor(topic_keys.ExtractorConfig())
    event = {"body_redacted": "PS2 launches on 2026-05-18",
             "source": "fictitious-future-producer-9000",
             "event_id": "ffp:abc",
             "source_ts": "2026-05-12T10:00:00Z"}
    out = ex.extract(event)
    assert out
    assert out[0].value_normalized == "2026-05-18"


def test_extractor_with_opportunistic_keys_emits_person_and_project():
    """An event with both a counterparty AND a project mention should
    emit claims for both topic keys."""
    ex = topic_keys.HeuristicExtractor(topic_keys.ExtractorConfig())
    out = ex.extract({
        "body_redacted": "PS2 launches on 2026-05-18",
        "source": "research-notes",
        "counterparty": "U099GSMAAU9",
    })
    topics = {c.topic_key for c in out}
    assert "project:ps2" in topics
    assert "person:u099gsmaau9" in topics


# --- Framework shape (AC-6) ------------------------------------------

def test_no_producer_branching_in_module():
    """The extractor MUST NOT inspect `event["source"]` and MUST NOT
    hardcode producer names in conditional logic. Mirrors the AC-6
    structural guarantee."""
    src = (REPO_ROOT / "agent" / "memory" / "topic_keys.py").read_text()
    # The forbidden patterns: producer-name compared/contained.
    for name in ("slack", "gmail", "agentry", "discord", "calendar", "teams"):
        for bad in (f'"{name}" ==', f'== "{name}"',
                    f"'{name}' ==", f"== '{name}'",
                    f'in ["{name}"', f'in ("{name}"',
                    f"in ['{name}'", f"in ('{name}'"):
            assert bad not in src, (
                f"topic_keys.py contains producer-name branch: {bad!r}"
            )


def test_module_does_not_import_producer_sdks():
    """Importing slack_sdk, agentry, etc. would couple the framework
    to a specific producer."""
    src = (REPO_ROOT / "agent" / "memory" / "topic_keys.py").read_text()
    for bad in ("import slack_sdk", "from slack_sdk",
                "import agentry", "from agentry",
                "import gmail", "from gmail",
                "import googleapiclient", "from googleapiclient"):
        assert bad not in src, f"topic_keys.py imports forbidden module: {bad!r}"


# --- Config loading --------------------------------------------------

def test_load_config_with_missing_dir_returns_defaults(tmp_path):
    """No config files → default predicate + stoplist + empty projects."""
    cfg = topic_keys.load_config(str(tmp_path / "nonexistent"))
    assert cfg.projects == {}
    assert "release-date" in cfg.predicates
    assert cfg.stoplist == topic_keys.DEFAULT_STOPLIST


def test_load_config_reads_projects_toml(tmp_path):
    cfg_dir = tmp_path / "brainstack"
    cfg_dir.mkdir()
    (cfg_dir / "projects.toml").write_text(
        '[projects]\nPS2 = ["ps2", "playstation-2"]\n'
    )
    cfg = topic_keys.load_config(str(cfg_dir))
    assert "PS2" in cfg.projects
    assert "ps2" in cfg.projects["PS2"]


def test_load_config_reads_stoplist_toml(tmp_path):
    cfg_dir = tmp_path / "brainstack"
    cfg_dir.mkdir()
    (cfg_dir / "stoplist.toml").write_text(
        '[stoplist]\nwords = ["FOO", "BAR"]\n'
    )
    cfg = topic_keys.load_config(str(cfg_dir))
    assert "FOO" in cfg.stoplist
    assert "BAR" in cfg.stoplist


def test_load_config_reads_channels_toml(tmp_path):
    cfg_dir = tmp_path / "brainstack"
    cfg_dir.mkdir()
    (cfg_dir / "channels.toml").write_text(
        '[channels]\nC0X = "network-leadership"\n'
    )
    cfg = topic_keys.load_config(str(cfg_dir))
    assert cfg.channels.get("C0X") == "network-leadership"


def test_load_config_raises_on_malformed_toml(tmp_path):
    """Codex PR2 P1 fix: a typo in projects.toml must surface to the
    operator, not silently flip allowlist → permissive mode."""
    cfg_dir = tmp_path / "brainstack"
    cfg_dir.mkdir()
    (cfg_dir / "projects.toml").write_text("[projects\nPS2 = broken")
    with pytest.raises(topic_keys.ExtractorConfigError):
        topic_keys.load_config(str(cfg_dir))


# --- Storage compatibility (claim_id contract) -----------------------

def test_extractor_emits_at_most_one_claim_per_slot_per_event():
    """Codex PR2 P1 fix: PR1 storage's claim_id = sha256(topic, subject,
    source_event_id) — value is NOT included. If one event emitted two
    claims for the same (topic, subject) with different values, both
    would share a claim_id and the storage layer would silently
    overwrite. The extractor MUST emit at most one per slot."""
    ex = topic_keys.HeuristicExtractor(topic_keys.ExtractorConfig())
    body = "PS2 launches on 2026-05-18 and ships on 2026-05-19"
    out = ex.extract({"body_redacted": body, "source": "research-notes",
                      "event_id": "n:1"})
    release_dates = [c for c in out
                     if c.topic_key == "project:ps2"
                     and c.claim_subject == "release-date"]
    assert len(release_dates) == 1  # first-match wins
    assert release_dates[0].value_normalized == "2026-05-18"


def test_decision_predicate_with_colon_matches():
    """Codex PR2 P1 fix: phrase 'decision:' ended with non-word char,
    and `\\b...\\b` failed to match. _boundary_regex now drops the
    trailing `\\b` for phrases ending in non-word chars."""
    matches = topic_keys._find_predicate_matches(
        "decision: we go with option A on 2026-05-18",
        topic_keys.DEFAULT_PREDICATES["decision"],
    )
    assert matches, "phrase 'decision:' should match in 'decision: we ...'"


# --- Date scope (codex PR2 P2) ---------------------------------------

def test_date_normalizer_rejects_us_slash_format():
    assert topic_keys._normalize_date("ships on 5/18/2026") is None


def test_date_normalizer_rejects_eu_dash_format():
    assert topic_keys._normalize_date("ships on 18-05-2026") is None


def test_date_normalizer_rejects_no_separator_format():
    assert topic_keys._normalize_date("ships on 20260518") is None


def test_date_normalizer_rejects_year_slash_month():
    assert topic_keys._normalize_date("ships on 2026/05/18") is None


# --- Negation lookbehind (codex PR2 P2 verification) ----------------

def test_negation_3_token_lookbehind_skips_correctly():
    """Negation 1 token back: skip. Negation 4+ tokens back: no skip."""
    body_skip = "PS2 not launching"
    matches_skip = topic_keys._find_predicate_matches(
        body_skip, topic_keys.DEFAULT_PREDICATES["release-date"],
    )
    assert matches_skip
    assert topic_keys._is_negated(body_skip, matches_skip[0][0])

    # 4 tokens between "not" and "launching" → no skip.
    body_keep = "we are not yet sure if PS2 will start launching anyway"
    matches_keep = topic_keys._find_predicate_matches(
        body_keep, topic_keys.DEFAULT_PREDICATES["release-date"],
    )
    assert matches_keep
    assert not topic_keys._is_negated(body_keep, matches_keep[0][0])


# --- Constructor purity (codex PR2 P2 fix) ---------------------------

def test_extractor_constructor_no_disk_io(tmp_path, monkeypatch):
    """HeuristicExtractor() must NOT read disk by default. Operators
    who want config call load_config() and pass it explicitly."""
    # Point HOME at an empty dir to be safe and verify no failure.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    ex = topic_keys.HeuristicExtractor()  # default config; no I/O
    assert ex.config.projects == {}
    assert "release-date" in ex.config.predicates


# --- Tightened multi-predicate cardinality ---------------------------

def test_extractor_multi_topic_multi_date_emits_first_match_per_slot():
    """One event with two project tokens × multiple dates: the extractor
    emits exactly ONE claim per (topic, subject) slot — the first
    matching date. Codex PR2 P1 fix ensures this matches storage."""
    ex = topic_keys.HeuristicExtractor(topic_keys.ExtractorConfig())
    out = ex.extract({
        "body_redacted": "PS2 and OKR launch on 2026-05-18 and ship on 2026-05-19",
        "source": "research-notes",
        "event_id": "n:1",
    })
    release_dates = sorted(
        [(c.topic_key, c.value_normalized) for c in out
         if c.claim_subject == "release-date"]
    )
    assert release_dates == [
        ("project:okr", "2026-05-18"),
        ("project:ps2", "2026-05-18"),
    ]


def test_permissive_mode_rejects_slack_channel_id_shape():
    """Real-world fix: Slack channel IDs (D0A8DQ7BP0U) and user IDs
    (U05LZF28SRH) have digits INTERLEAVED with letters. They must NOT
    become project:* topic keys in permissive mode."""
    cfg = topic_keys.ExtractorConfig()
    body = "discussion in D0A8DQ7BP0U with U05LZF28SRH about PS2 launch"
    keys = topic_keys._topic_keys_from_body(body, cfg)
    assert "project:d0a8dq7bp0u" not in keys
    assert "project:u05lzf28srh" not in keys
    assert "project:ps2" in keys


def test_status_predicate_rejects_bare_done_in_casual_chat():
    """Real-world fix: the body 'Any idea how can i get it done' must
    NOT produce a status=done claim. Tightened predicate requires
    explicit shapes ('status:', 'the status is', 'currently blocked')."""
    ex = topic_keys.HeuristicExtractor(topic_keys.ExtractorConfig())
    out = ex.extract({
        "body_redacted": "Any idea how can i get it done",
        "source": "slack", "event_id": "x", "source_ts": "1700000000.0",
        "counterparty": "U123ABC",
    })
    status_claims = [c for c in out if c.claim_subject == "status"]
    assert status_claims == []


def test_status_predicate_still_matches_explicit_shape():
    """Tightened status predicate must still catch real status reports."""
    ex = topic_keys.HeuristicExtractor(topic_keys.ExtractorConfig())
    out = ex.extract({
        "body_redacted": "PS2 status: blocked on the data migration",
        "source": "research-notes", "event_id": "n:1",
        "source_ts": "1700000000.0",
    })
    status_claims = [c for c in out if c.claim_subject == "status"]
    assert any(c.value_normalized == "blocked" for c in status_claims)


def test_load_config_overrides_predicate(tmp_path):
    cfg_dir = tmp_path / "brainstack"
    cfg_dir.mkdir()
    (cfg_dir / "extractors.toml").write_text(
        '[predicates.custom-pred]\n'
        'match = ["xyz"]\n'
        'normalizer = "freeform-2k"\n'
    )
    cfg = topic_keys.load_config(str(cfg_dir))
    assert "custom-pred" in cfg.predicates
    # Defaults still present.
    assert "release-date" in cfg.predicates
