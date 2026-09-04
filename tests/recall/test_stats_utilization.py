"""Tests for `recall stats --utilization` — did anyone OPEN the docs?

`recall stats` can only say how many docs auto-recall injected. That is
not the same question as whether the injection helped. This module pins
the join that answers the harder one: take every schema-1.2 AutoRecall
`hit` (which carries `x_paths`, the brain-relative paths it injected),
find the Claude Code transcript for that session, locate the injection
attachment near the event timestamp, and look at what the model did
next. A doc counts as USED when a later tool call in the same session
reads it — `Read`/`Grep`/`Glob` on the path, or a `Bash` command that
mentions it.

Two outputs:
  (a) a mechanical percentage — injected docs later opened in-session;
  (b) a JSON sample of cases (prompt, injected docs, response) shaped
      exactly like the ad-hoc `tools/sample_utilization.py` script, so
      an LLM judge can grade relevance on the same input.

Legacy hits (schema 1.1, or 1.2 without `x_paths`) cannot be joined —
they never logged which docs they injected — so they are counted and
reported separately rather than silently scored as unused.

Everything here is hermetic: fake transcripts and a fake events log
under `tmp_path`. Nothing reads ~/.claude or ~/.agent.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

DOC_A = "memory/semantic/lessons/atomic-writes.md"
DOC_B = "imports/claude/plans/corpus-stats.md"
DOC_C = "memory/semantic/digests/2026-09-01__unused__abc123.md"

PROMPT = "please explain how the atomic write helper in the brain avoids torn files"
SID_A = "aaaaaaaa-1111-2222-3333-444444444444"
SID_B = "bbbbbbbb-1111-2222-3333-444444444444"
PROJECT = "-Users-x-proj"

MINUTE_MS = 60_000


def _now_ms() -> int:
    return int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)


def _iso(ts_ms: int) -> str:
    dt = datetime.datetime.fromtimestamp(ts_ms / 1000, tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# Transcript record shapes — copied from real ~/.claude/projects lines
# ---------------------------------------------------------------------------


def _user(uuid: str, parent: str | None, ts_ms: int, text: str,
          sid: str = SID_A) -> dict:
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": _iso(ts_ms),
        "sessionId": sid,
        "isSidechain": False,
        "message": {"role": "user", "content": text},
    }


def _assistant(uuid: str, parent: str | None, ts_ms: int, blocks: list[dict],
               sid: str = SID_A, sidechain: bool = False) -> dict:
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": _iso(ts_ms),
        "sessionId": sid,
        "isSidechain": sidechain,
        "message": {"role": "assistant", "content": blocks},
    }


def _attachment(uuid: str, parent: str | None, ts_ms: int, content: str,
                sid: str = SID_A, hook_ms: int = 721) -> dict:
    return {
        "type": "attachment",
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": _iso(ts_ms),
        "sessionId": sid,
        "attachment": {
            "hookName": "UserPromptSubmit",
            "content": content,
            "durationMs": hook_ms,
        },
    }


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def _tool_use(name: str, inp: dict) -> dict:
    return {"type": "tool_use", "name": name, "input": inp}


class World:
    """A fake brain + transcripts dir + events log under tmp_path."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.brain = tmp_path / "brain"
        for rel in (DOC_A, DOC_B, DOC_C):
            p = self.brain / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"# {Path(rel).stem}\n\nbody of {rel}\n")
        self.projects = tmp_path / "projects"
        self.projects.mkdir()
        self.logs = tmp_path / "logs"
        self.logs.mkdir()
        self.log = self.logs / "events.log.jsonl"
        self.log.touch()

    def abs_path(self, rel: str) -> str:
        return str(self.brain / rel)

    def block(self, docs: list[tuple[str, float]], *, hook_ms: int = 721,
              absolute: bool = True) -> str:
        """Render an injection block the way the hook renders it: a header
        line the join regex matches, then one `## <path> (score X)`
        section per doc wrapped in start/end markers."""
        parts = [
            "<system-reminder>\n",
            f"auto-recall: {len(docs)} docs surfaced in {hook_ms}ms · brainstack\n",
        ]
        for i, (rel, score) in enumerate(docs, start=1):
            shown = self.abs_path(rel) if absolute else rel
            parts.append(
                f"## {shown} (score {score:.2f}) · provenance: none\n"
                f"[recall-doc-{i}-start]\n"
                f"# {Path(rel).stem}\n\nbody of {rel}\n"
                f"[recall-doc-{i}-end]\n"
            )
        parts.append("</system-reminder>")
        return "".join(parts)

    def write_session(self, sid: str, records: list[dict], *,
                      project: str = PROJECT) -> Path:
        directory = self.projects / project
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{sid}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
        return path

    def write_event(self, *, sid: str, ts_ms: int, paths: list[str] | None = None,
                    outcome: str = "hit", schema_version: str = "1.2",
                    event: str = "AutoRecall", **extra) -> dict:
        paths = list(paths or [])
        rec: dict = {
            "schema_version": schema_version,
            "ts_ms": ts_ms,
            "event": event,
            "session_id": sid,
            "turn": 0,
        }
        if event == "AutoRecall":
            rec["x_outcome"] = outcome
            rec["x_path"] = "daemon"
            rec["x_k_returned"] = len(paths)
            rec["x_latency_ms"] = 240
            if schema_version == "1.2" and paths:
                rec["x_paths"] = paths
                rec["x_paths_truncated"] = False
        rec.update(extra)
        with self.log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
        return rec

    def one_hit_session(self, *, t0: int, docs: list[tuple[str, float]],
                        after: list[list[dict]] | None = None,
                        before: list[list[dict]] | None = None,
                        prompt: str = PROMPT, sid: str = SID_A,
                        project: str = PROJECT,
                        event_offset_ms: int = 0,
                        absolute_paths: bool = True) -> Path:
        """user prompt -> [pre-injection turns] -> injection attachment ->
        [post-injection turns], plus the matching AutoRecall event."""
        records = [_user("u-user", None, t0, prompt, sid)]
        parent = "u-user"
        for i, blocks in enumerate(before or []):
            uuid = f"u-pre-{i}"
            records.append(_assistant(uuid, parent, t0 + 100 + i, blocks, sid))
            parent = uuid
        att_ts = t0 + 1000
        records.append(_attachment(
            "u-att", parent, att_ts,
            self.block(docs, absolute=absolute_paths), sid))
        parent = "u-att"
        for i, blocks in enumerate(after or []):
            uuid = f"u-post-{i}"
            records.append(_assistant(uuid, parent, att_ts + 1000 + i, blocks, sid))
            parent = uuid
        path = self.write_session(sid, records, project=project)
        self.write_event(sid=sid, ts_ms=att_ts + event_offset_ms,
                         paths=[rel for rel, _ in docs])
        return path


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


@pytest.fixture
def t0() -> int:
    """Base timestamp for fixture events: 30 minutes ago.

    Comfortably inside every window these tests use. The tightest is the
    CLI's `--since 1h`, which leaves ~30 minutes of slack. Anchoring at
    exactly the edge (`now - 1h`) made that case flaky: the CLI re-reads
    the clock when it parses `--since`, so on a slow runner its cutoff
    slides past the event and the hit vanishes. A test that needs a
    window shorter than 30 minutes must move this base, not shave the
    margin.
    """
    return _now_ms() - 30 * MINUTE_MS


def _compute(world: World, **kwargs):
    from recall.utilization import compute_utilization
    params = dict(brain_root=world.brain, since_ts_ms=None)
    params.update(kwargs)
    return compute_utilization(world.log, world.projects, **params)


# ---------------------------------------------------------------------------
# (a) mechanical metric: injected docs later opened in-session
# ---------------------------------------------------------------------------


class TestMechanicalUse:
    def test_read_of_injected_path_counts_as_used(self, world: World, t0: int):
        world.one_hit_session(
            t0=t0,
            docs=[(DOC_A, 0.81), (DOC_C, 0.55)],
            after=[
                [_text("Let me read the lesson."),
                 _tool_use("Read", {"file_path": world.abs_path(DOC_A)})],
            ],
        )
        report = _compute(world)
        assert report.hit_events == 1
        assert report.joined_events == 1
        assert report.unjoined_events == 0
        assert report.injected_docs == 2
        assert report.used_docs == 1
        assert report.used_by_tool == {"Read": 1}
        assert report.used_pct == pytest.approx(50.0)
        assert report.used_docs_top == [(DOC_A, 1)]

    def test_bash_cat_counts_as_used(self, world: World, t0: int):
        world.one_hit_session(
            t0=t0,
            docs=[(DOC_B, 0.62)],
            after=[[_tool_use("Bash", {"command": f"cat {DOC_B} | head -40"})]],
        )
        report = _compute(world)
        assert report.used_docs == 1
        assert report.used_by_tool == {"Bash": 1}

    def test_bash_absolute_path_counts_as_used(self, world: World, t0: int):
        world.one_hit_session(
            t0=t0,
            docs=[(DOC_B, 0.62)],
            after=[[_tool_use("Bash", {"command": f"wc -l {world.abs_path(DOC_B)}"})]],
        )
        assert _compute(world).used_docs == 1

    def test_grep_path_counts_as_used(self, world: World, t0: int):
        world.one_hit_session(
            t0=t0,
            docs=[(DOC_A, 0.81)],
            after=[[_tool_use("Grep", {"pattern": "atomic",
                                       "path": world.abs_path(DOC_A)})]],
        )
        report = _compute(world)
        assert report.used_docs == 1
        assert report.used_by_tool == {"Grep": 1}

    def test_tool_call_without_path_input_is_ignored(self, world: World, t0: int):
        """`Grep` without a `path` searches the cwd — no evidence the
        injected doc was opened, and it must not crash the join."""
        world.one_hit_session(
            t0=t0,
            docs=[(DOC_A, 0.81)],
            after=[[_tool_use("Grep", {"pattern": "atomic"})]],
        )
        assert _compute(world).used_docs == 0

    def test_unrelated_read_not_counted(self, world: World, t0: int):
        world.one_hit_session(
            t0=t0,
            docs=[(DOC_A, 0.81)],
            after=[[_tool_use("Read", {"file_path": "/tmp/somewhere-else.md"})]],
        )
        report = _compute(world)
        assert report.used_docs == 0
        assert report.used_pct == pytest.approx(0.0)
        assert report.used_by_tool == {}

    def test_tool_use_before_injection_not_counted(self, world: World, t0: int):
        """The model opening a doc BEFORE auto-recall injected it is not
        evidence that the injection helped — it is the opposite."""
        world.one_hit_session(
            t0=t0,
            docs=[(DOC_A, 0.81)],
            before=[[_tool_use("Read", {"file_path": world.abs_path(DOC_A)})]],
            after=[[_text("Here is the answer.")]],
        )
        assert _compute(world).used_docs == 0

    def test_absolute_block_path_matches_relative_x_path(self, world: World, t0: int):
        """Transcript tool inputs are absolute and often carry a line
        reference; `x_paths` is brain-relative. Both sides normalize."""
        world.one_hit_session(
            t0=t0,
            docs=[(DOC_A, 0.81)],
            after=[[_tool_use("Read", {"file_path": world.abs_path(DOC_A) + ":12"})]],
        )
        assert _compute(world).used_docs == 1

    def test_norm_brain_path_strips_root_and_line_refs(self, world: World):
        from recall.utilization import norm_brain_path
        assert norm_brain_path(world.abs_path(DOC_A), world.brain) == DOC_A
        assert norm_brain_path(world.abs_path(DOC_A) + ":12", world.brain) == DOC_A
        assert norm_brain_path(world.abs_path(DOC_A) + ":12:4", world.brain) == DOC_A
        assert norm_brain_path(DOC_A, world.brain) == DOC_A
        # A path outside the brain root is returned unchanged (minus the ref)
        assert norm_brain_path("/tmp/other.md:3", world.brain) == "/tmp/other.md"

    def test_repeat_use_of_same_doc_counted_once(self, world: World, t0: int):
        world.one_hit_session(
            t0=t0,
            docs=[(DOC_A, 0.81)],
            after=[
                [_tool_use("Read", {"file_path": world.abs_path(DOC_A)})],
                [_tool_use("Grep", {"path": world.abs_path(DOC_A), "pattern": "x"})],
            ],
        )
        report = _compute(world)
        assert report.used_docs == 1
        assert report.used_by_tool == {"Read": 1}

    def test_sidechain_tool_calls_count(self, world: World, t0: int):
        """A subagent launched mid-turn reading the injected doc is still
        the injection paying off."""
        t_att = t0 + 1000
        world.write_session(SID_A, [
            _user("u-user", None, t0, PROMPT),
            _attachment("u-att", "u-user", t_att, world.block([(DOC_A, 0.81)])),
            _assistant("u-sub", "u-att", t_att + 1000,
                       [_tool_use("Read", {"file_path": world.abs_path(DOC_A)})],
                       sidechain=True),
        ])
        world.write_event(sid=SID_A, ts_ms=t_att, paths=[DOC_A])
        assert _compute(world).used_docs == 1


class TestJoin:
    def test_missing_transcript_is_unjoined(self, world: World, t0: int):
        world.write_event(sid=SID_B, ts_ms=t0, paths=[DOC_A, DOC_B])
        report = _compute(world)
        assert report.hit_events == 1
        assert report.joined_events == 0
        assert report.unjoined_events == 1
        # usage is unknown, not zero — the docs stay out of the denominator
        assert report.injected_docs == 0
        assert report.used_pct == pytest.approx(0.0)

    def test_subagent_transcripts_ignored(self, world: World, t0: int):
        """`<slug>/subagents/<sid>.jsonl` is a sub-agent's copy, not the
        session the hook fired in. Joining to it would attribute the
        subagent's reads to the main session."""
        records = [
            _user("u-user", None, t0, PROMPT),
            _attachment("u-att", "u-user", t0 + 1000, world.block([(DOC_A, 0.81)])),
            _assistant("u-a", "u-att", t0 + 2000,
                       [_tool_use("Read", {"file_path": world.abs_path(DOC_A)})]),
        ]
        world.write_session(SID_A, records, project="subagents")
        world.write_session(SID_A, records, project=f"{PROJECT}/subagents")
        world.write_event(sid=SID_A, ts_ms=t0 + 1000, paths=[DOC_A])

        report = _compute(world)
        assert report.joined_events == 0
        assert report.unjoined_events == 1
        assert report.used_docs == 0

    def test_find_transcript_skips_subagents(self, world: World):
        from recall.utilization import find_transcript
        world.write_session(SID_A, [], project="subagents")
        assert find_transcript(world.projects, SID_A) is None
        real = world.write_session(SID_A, [], project=PROJECT)
        assert find_transcript(world.projects, SID_A) == real

    def test_nearest_attachment_within_window(self, world: World, t0: int):
        """Two injections in one session. The event must bind to the
        NEAREST attachment; a read that happened before it belongs to the
        earlier injection and is not credited here."""
        t_att1 = t0 + 1000
        t_att2 = t_att1 + 60_000
        world.write_session(SID_A, [
            _user("u1", None, t0, PROMPT),
            _attachment("u-att1", "u1", t_att1, world.block([(DOC_A, 0.81)])),
            _assistant("u-a", "u-att1", t_att1 + 1000,
                       [_tool_use("Read", {"file_path": world.abs_path(DOC_A)})]),
            _user("u2", "u-a", t_att2 - 500,
                  "now compare that with the corpus stats plan please"),
            _attachment("u-att2", "u2", t_att2, world.block([(DOC_A, 0.79)])),
            _assistant("u-b", "u-att2", t_att2 + 1000, [_text("Done.")]),
        ])
        # ts_ms sits 1s from attachment 2 and 59s from attachment 1
        world.write_event(sid=SID_A, ts_ms=t_att2 + 1000, paths=[DOC_A])

        report = _compute(world)
        assert report.joined_events == 1
        assert report.used_docs == 0

    def test_attachment_outside_window_is_unjoined(self, world: World, t0: int):
        world.one_hit_session(
            t0=t0,
            docs=[(DOC_A, 0.81)],
            after=[[_tool_use("Read", {"file_path": world.abs_path(DOC_A)})]],
            event_offset_ms=5 * MINUTE_MS,  # > the 120s default window
        )
        report = _compute(world)
        assert report.joined_events == 0
        assert report.unjoined_events == 1

    def test_legacy_hits_ignored_counted(self, world: World, t0: int):
        """1.1 hits never logged which docs they injected, so they cannot
        be scored. Report them instead of scoring them as unused."""
        world.one_hit_session(
            t0=t0,
            docs=[(DOC_A, 0.81)],
            after=[[_tool_use("Read", {"file_path": world.abs_path(DOC_A)})]],
        )
        world.write_event(sid=SID_A, ts_ms=t0 + 1000, paths=[DOC_A],
                          schema_version="1.1")
        report = _compute(world)
        assert report.hit_events == 1
        assert report.legacy_hits_ignored == 1
        assert report.used_docs == 1

    def test_non_hit_outcomes_ignored(self, world: World, t0: int):
        world.write_event(sid=SID_A, ts_ms=t0, outcome="miss")
        world.write_event(sid=SID_A, ts_ms=t0, outcome="dedup")
        world.write_event(sid=SID_A, ts_ms=t0, outcome="skip")
        world.write_event(sid=SID_A, ts_ms=t0, event="PostToolUse")
        report = _compute(world)
        assert report.hit_events == 0
        assert report.legacy_hits_ignored == 0
        assert report.injected_docs == 0

    def test_since_window_respected(self, world: World, t0: int):
        old = t0 - 30 * 24 * 60 * MINUTE_MS
        world.one_hit_session(t0=t0, docs=[(DOC_A, 0.81)],
                              after=[[_text("recent")]])
        world.write_event(sid=SID_B, ts_ms=old, paths=[DOC_B])
        report = _compute(world, since_ts_ms=t0 - 60 * MINUTE_MS)
        assert report.hit_events == 1
        assert report.since_ts_ms == t0 - 60 * MINUTE_MS

    def test_empty_world_is_all_zero(self, world: World):
        report = _compute(world)
        assert report.hit_events == 0
        assert report.injected_docs == 0
        assert report.used_docs == 0
        assert report.used_pct == pytest.approx(0.0)
        assert report.sample_written == 0
        assert report.sample_path is None

    def test_malformed_transcript_line_skipped(self, world: World, t0: int):
        path = world.one_hit_session(
            t0=t0,
            docs=[(DOC_A, 0.81)],
            after=[[_tool_use("Read", {"file_path": world.abs_path(DOC_A)})]],
        )
        with path.open("a", encoding="utf-8") as fh:
            fh.write("not json at all\n{\"truncated\":\n")
        assert _compute(world).used_docs == 1


class TestParseInjectionBlock:
    def test_parses_path_score_and_excerpt(self, world: World):
        from recall.utilization import parse_injection_block
        content = world.block([(DOC_A, 0.81), (DOC_B, 0.62)])
        docs = parse_injection_block(content)
        assert [d["path"] for d in docs] == [world.abs_path(DOC_A),
                                             world.abs_path(DOC_B)]
        assert docs[0]["score"] == pytest.approx(0.81)
        assert "body of " + DOC_A in docs[0]["excerpt"]

    def test_block_without_docs_returns_empty(self):
        from recall.utilization import parse_injection_block
        assert parse_injection_block("auto-recall: 0 docs surfaced in 12ms") == []


# ---------------------------------------------------------------------------
# (b) LLM-judge sample
# ---------------------------------------------------------------------------


class TestSample:
    def test_sample_shape_matches_sample_utilization_tool(self, world: World,
                                                          t0: int, tmp_path: Path):
        """The sample feeds the same judge prompt the ad-hoc script fed,
        so the per-case keys must match it exactly."""
        world.one_hit_session(
            t0=t0,
            docs=[(DOC_A, 0.81), (DOC_B, 0.62)],
            after=[[_text("Looking now."),
                    _tool_use("Read", {"file_path": world.abs_path(DOC_A)})]],
        )
        out = tmp_path / "sample.json"
        report = _compute(world, sample_out=out)
        assert report.sample_written == 1
        assert report.sample_path == str(out)

        cases = json.loads(out.read_text())
        assert isinstance(cases, list) and len(cases) == 1
        case = cases[0]
        assert set(case) == {"session_id", "project_dir", "ts", "hook_ms",
                             "docset", "docs", "prompt", "response"}
        assert case["session_id"] == SID_A
        assert case["project_dir"] == PROJECT
        assert case["hook_ms"] == 721
        assert case["prompt"] == PROMPT
        assert case["docset"] == "|".join(sorted(
            [world.abs_path(DOC_A), world.abs_path(DOC_B)]))
        assert set(case["docs"][0]) == {"path", "score", "excerpt"}
        assert case["docs"][0]["path"] == world.abs_path(DOC_A)
        assert case["docs"][0]["score"] == pytest.approx(0.81)
        assert "body of " + DOC_A in case["docs"][0]["excerpt"]
        assert "[text] Looking now." in case["response"]
        assert "[tool_use Read]" in case["response"]

    def test_prompt_walks_parent_chain(self, world: World, t0: int, tmp_path: Path):
        """The attachment's parent is not always the human turn — walk up
        the `parentUuid` chain until a real user message appears."""
        t_att = t0 + 1000
        world.write_session(SID_A, [
            _user("u-user", None, t0, PROMPT),
            _assistant("u-mid", "u-user", t0 + 500, [_text("thinking")]),
            _attachment("u-att", "u-mid", t_att, world.block([(DOC_A, 0.81)])),
            _assistant("u-post", "u-att", t_att + 1000, [_text("Answering.")]),
        ])
        world.write_event(sid=SID_A, ts_ms=t_att, paths=[DOC_A])
        out = tmp_path / "sample.json"
        _compute(world, sample_out=out)
        assert json.loads(out.read_text())[0]["prompt"] == PROMPT

    def test_prompt_forward_scan_when_chain_is_broken(self, world: World, t0: int,
                                                      tmp_path: Path):
        """Compacted transcripts lose `parentUuid`. Fall forward to the
        next human turn rather than dropping the case."""
        t_att = t0 + 1000
        world.write_session(SID_A, [
            _attachment("u-att", None, t_att, world.block([(DOC_A, 0.81)])),
            _user("u-user", None, t_att + 100, PROMPT),
            _assistant("u-post", "u-user", t_att + 1000, [_text("Answering.")]),
        ])
        world.write_event(sid=SID_A, ts_ms=t_att, paths=[DOC_A])
        out = tmp_path / "sample.json"
        _compute(world, sample_out=out)
        assert json.loads(out.read_text())[0]["prompt"] == PROMPT

    def test_response_stops_at_next_human_prompt(self, world: World, t0: int,
                                                 tmp_path: Path):
        t_att = t0 + 1000
        world.write_session(SID_A, [
            _user("u-user", None, t0, PROMPT),
            _attachment("u-att", "u-user", t_att, world.block([(DOC_A, 0.81)])),
            _assistant("u-a", "u-att", t_att + 1000, [_text("FIRST ANSWER here.")]),
            _user("u-user2", "u-a", t_att + 2000,
                  "different question entirely about the sync quarantine path"),
            _assistant("u-b", "u-user2", t_att + 3000, [_text("SECOND ANSWER here.")]),
        ])
        world.write_event(sid=SID_A, ts_ms=t_att, paths=[DOC_A])
        out = tmp_path / "sample.json"
        _compute(world, sample_out=out)
        response = json.loads(out.read_text())[0]["response"]
        assert "FIRST ANSWER" in response
        assert "SECOND ANSWER" not in response

    def test_slash_command_prompts_are_skipped(self, world: World, t0: int,
                                               tmp_path: Path):
        """`/agent-team ...` and `# note` turns are commands, not
        questions — judging retrieval relevance against them is noise."""
        world.one_hit_session(
            t0=t0, docs=[(DOC_A, 0.81)],
            prompt="/agent-team please run the full cycle on the stats slice now",
            after=[[_text("Starting.")]],
        )
        out = tmp_path / "sample.json"
        report = _compute(world, sample_out=out)
        # still counted mechanically...
        assert report.hit_events == 1
        assert report.joined_events == 1
        # ...but never sampled for the judge
        assert report.sample_written == 0
        assert json.loads(out.read_text()) == []

    def test_sample_n_caps_the_written_cases(self, world: World, t0: int,
                                             tmp_path: Path):
        for i in range(3):
            sid = f"cccccccc-0000-0000-0000-00000000000{i}"
            world.one_hit_session(
                t0=t0 + i * 10 * MINUTE_MS,
                docs=[(DOC_A, 0.81)],
                sid=sid,
                after=[[_text(f"Answer {i} for the caller.")]],
            )
        out = tmp_path / "sample.json"
        report = _compute(world, sample_out=out, sample_n=2)
        assert report.sample_written == 2
        assert len(json.loads(out.read_text())) == 2

    def test_no_sample_written_without_sample_out(self, world: World, t0: int):
        world.one_hit_session(t0=t0, docs=[(DOC_A, 0.81)],
                              after=[[_text("Answering the question.")]])
        report = _compute(world)
        assert report.sample_written == 0
        assert report.sample_path is None

    def test_sample_written_atomically(self, world: World, t0: int, tmp_path: Path):
        """Written via temp + replace, so a crash never leaves a partial
        JSON file the judge would choke on."""
        from recall.utilization import write_sample
        target = tmp_path / "out" / "sample.json"
        target.parent.mkdir()
        cases = [{"session_id": "s1", "docset": "d1", "prompt": "p",
                  "response": "r", "docs": [], "ts": "t", "hook_ms": 1,
                  "project_dir": "proj"}]
        assert write_sample(cases, target) == 1
        assert json.loads(target.read_text()) == cases
        assert sorted(p.name for p in target.parent.iterdir()) == ["sample.json"]

    def test_select_sample_dedupe_rules_seeded(self):
        """One case per (session, docset), at most 2 per session and 3
        per docset — otherwise one chatty session dominates the judge set.
        Seeded, so re-running produces the same sample."""
        from recall.utilization import select_sample
        cases = [
            {"session_id": "s1", "docset": "d1", "prompt": "a"},
            {"session_id": "s1", "docset": "d1", "prompt": "b"},
            {"session_id": "s1", "docset": "d2", "prompt": "c"},
            {"session_id": "s1", "docset": "d3", "prompt": "d"},
            {"session_id": "s2", "docset": "d1", "prompt": "e"},
            {"session_id": "s3", "docset": "d1", "prompt": "f"},
            {"session_id": "s4", "docset": "d1", "prompt": "g"},
            {"session_id": "s5", "docset": "d4", "prompt": "h"},
        ]
        picked = select_sample(list(cases), 10, 20260904)
        keys = [(c["session_id"], c["docset"]) for c in picked]
        assert len(keys) == len(set(keys))
        for sid in {c["session_id"] for c in picked}:
            assert sum(1 for c in picked if c["session_id"] == sid) <= 2
        for docset in {c["docset"] for c in picked}:
            assert sum(1 for c in picked if c["docset"] == docset) <= 3
        # deterministic for a fixed seed
        assert select_sample(list(cases), 10, 20260904) == picked
        # and the cap is respected
        assert len(select_sample(list(cases), 2, 20260904)) == 2


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def _utilization_report(**overrides):
    from recall.utilization import UtilizationReport
    fields = dict(
        since_ts_ms=int(datetime.datetime(
            2026, 8, 21, tzinfo=datetime.timezone.utc).timestamp() * 1000),
        hit_events=101,
        joined_events=97,
        unjoined_events=4,
        legacy_hits_ignored=414,
        injected_docs=301,
        used_docs=27,
        used_pct=100 * 27 / 301,
        used_by_tool={"Read": 20, "Grep": 4, "Bash": 3},
        used_docs_top=[("memory/semantic/lessons/foo.md", 6),
                       ("imports/claude/plans/bar.md", 3)],
        sample_written=24,
        sample_path="/Users/me/.agent/runtime/logs/utilization_sample.json",
    )
    fields.update(overrides)
    return UtilizationReport(**fields)


class TestRenderUtilization:
    def test_render_matches_plan_sample(self):
        from recall.utilization import render_utilization
        out = render_utilization(_utilization_report())
        assert "brainstack: auto-recall utilization (since 2026-08-21)" in out
        assert ("  Hits joined:    97 / 101 schema-1.2 hits had a transcript"
                " (4 unjoined)") in out
        assert ("  Docs injected:  301 · opened later in-session: 27 (9.0%)"
                " — Read 20, Grep 4, Bash 3") in out
        assert ("  Most opened:    memory/semantic/lessons/foo.md (6),"
                " imports/claude/plans/bar.md (3)") in out
        assert "  Legacy hits ignored (no x_paths): 414" in out
        assert ("  Sample:         24 cases →"
                " /Users/me/.agent/runtime/logs/utilization_sample.json"
                " (LLM-judge input)") in out

    def test_render_omits_optional_lines(self):
        from recall.utilization import render_utilization
        out = render_utilization(_utilization_report(
            legacy_hits_ignored=0, sample_written=0, sample_path=None,
            used_docs_top=[]))
        assert "Legacy hits ignored" not in out
        assert "Sample:" not in out
        assert "Most opened" not in out
        # the load-bearing lines stay
        assert "Docs injected:  301" in out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def stats_cli(world: World, monkeypatch):
    """`recall stats` reads its log path from RuntimeConfig — point it at
    the fake world so no test can touch the developer's real log."""
    cfg = world.tmp / "pyproject.toml"
    cfg.write_text(
        "[tool.recall.runtime]\n"
        f'log_dir = "{world.logs}"\n'
    )
    monkeypatch.setenv("RECALL_RUNTIME_CONFIG", str(cfg))
    monkeypatch.setenv("BRAIN_ROOT", str(world.brain))
    monkeypatch.setenv("XDG_CACHE_HOME", str(world.tmp / "xdg-cache"))
    return CliRunner()


class TestUtilizationCli:
    def test_cli_utilization_defaults_since_14d_and_writes_sample(
            self, world: World, t0: int, stats_cli: CliRunner):
        from recall.cli import app
        world.one_hit_session(
            t0=t0,
            docs=[(DOC_A, 0.81)],
            after=[[_text("Reading the lesson now."),
                    _tool_use("Read", {"file_path": world.abs_path(DOC_A)})]],
        )
        # 30 days old — 16 days clear of the implicit 14d cutoff, so the
        # exclusion can't hinge on how long the run took
        world.write_event(sid=SID_B, ts_ms=t0 - 30 * 24 * 60 * MINUTE_MS,
                          paths=[DOC_B])

        result = stats_cli.invoke(app, [
            "stats", "--utilization",
            "--transcripts-dir", str(world.projects),
            "--brain-root", str(world.brain),
        ])
        assert result.exit_code == 0, result.output
        assert "auto-recall utilization" in result.output
        # only the in-window hit counted → the 14d default applied
        assert "1 / 1 schema-1.2 hits" in result.output
        assert "opened later in-session: 1" in result.output
        # The sample holds raw prompt/response text, so by default it lands in
        # the recall cache dir, never under the brain root (sync.sh would push
        # anything under runtime/logs to the remote).
        from recall.config import cache_dir
        default_sample = cache_dir() / "utilization_sample.json"
        assert default_sample.is_file()
        assert not default_sample.is_relative_to(world.brain)
        assert not (world.logs / "utilization_sample.json").exists()
        assert len(json.loads(default_sample.read_text())) == 1

    def test_cli_utilization_json(self, world: World, t0: int,
                                  stats_cli: CliRunner, tmp_path: Path):
        from recall.cli import app
        world.one_hit_session(
            t0=t0,
            docs=[(DOC_A, 0.81), (DOC_C, 0.55)],
            after=[[_tool_use("Read", {"file_path": world.abs_path(DOC_A)})]],
        )
        out = tmp_path / "sample.json"
        result = stats_cli.invoke(app, [
            "stats", "--utilization", "--json",
            "--transcripts-dir", str(world.projects),
            "--brain-root", str(world.brain),
            "--sample-out", str(out),
            "--sample-n", "5",
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        for key in ("since_ts_ms", "hit_events", "joined_events",
                    "unjoined_events", "legacy_hits_ignored", "injected_docs",
                    "used_docs", "used_pct", "used_by_tool", "used_docs_top",
                    "sample_written", "sample_path"):
            assert key in data, f"missing utilization field: {key}"
        assert data["hit_events"] == 1
        assert data["injected_docs"] == 2
        assert data["used_docs"] == 1
        assert data["used_pct"] == pytest.approx(50.0)
        assert data["used_by_tool"] == {"Read": 1}
        assert data["used_docs_top"] == [[DOC_A, 1]]
        assert data["sample_path"] == str(out)
        # --utilization REPLACES the base report rather than appending to it
        assert "fired_count" not in data
        assert "score_distribution" not in data

    def test_cli_utilization_explicit_since(self, world: World, t0: int,
                                            stats_cli: CliRunner):
        """An explicit `--since` overrides the implicit 14-day default.
        The in-window event is 30 minutes old and the excluded one is 3
        hours old, so neither sits near the 1-hour cutoff when the CLI
        re-reads the clock."""
        from recall.cli import app
        world.one_hit_session(t0=t0, docs=[(DOC_A, 0.81)],
                              after=[[_text("Answering the question.")]])
        world.write_event(sid=SID_B, ts_ms=t0 - 150 * MINUTE_MS, paths=[DOC_B])
        result = stats_cli.invoke(app, [
            "stats", "--utilization", "--json", "--since", "1h",
            "--transcripts-dir", str(world.projects),
            "--brain-root", str(world.brain),
        ])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["hit_events"] == 1

    def test_cli_utilization_empty_log_is_clean_exit(self, world: World,
                                                     stats_cli: CliRunner):
        from recall.cli import app
        result = stats_cli.invoke(app, [
            "stats", "--utilization",
            "--transcripts-dir", str(world.projects),
            "--brain-root", str(world.brain),
        ])
        assert result.exit_code == 0, result.output
        assert "utilization" in result.output
