#!/usr/bin/env python3
"""Build a fully synthetic demo brain for the recorded demo (demo/demo.tape).

Usage:
    python3 demo/make_demo_brain.py [TARGET_DIR]    # default: ./demo/brain

Creates:
    TARGET_DIR/
      memory/semantic/lessons/*.md     6 synthetic lessons (real frontmatter shape;
                                       the hero lesson carries full remember-style
                                       provenance so `recall trace` shows a chain)
      memory/semantic/digests/*.md     1 synthetic session digest the hero lesson's
                                       session_id resolves to in `recall trace`
      memory/candidates/*.json         2 staged dream candidates (real triage schema)
      imports/*.md                     1 synthetic imported note (the default config
                                       indexes an imports tier; an empty collection
                                       crashes embedded-qdrant hybrid queries)
      PENDING_REVIEW.md                summary consumed by `recall pending`
      runtime.toml                     runtime config: auto-recall enabled (for the
                                       hook beat) + stats pointed at the demo log
      runtime/logs/events.log.jsonl    synthetic AutoRecall telemetry for `recall stats`

Every byte of content is synthetic. Placeholder org/person names only
(Acme, Alice). Safe to publish in a recorded GIF.

Point recall at it with:
    export BRAIN_ROOT="$PWD/demo/brain"
(`recall query` reads $BRAIN_ROOT/memory; `recall pending` reads
$BRAIN_ROOT/PENDING_REVIEW.md; `recall stats` reads the log_dir from
$RECALL_RUNTIME_CONFIG.)
"""
from __future__ import annotations

import datetime
import json
import random
import sys
from pathlib import Path

# Must match runtime/core/events.py EVENT_LOG_SCHEMA_VERSION. Hardcoded so
# this script stays standalone (runnable without PYTHONPATH=repo-root); the
# import below picks up the live value when the repo is importable.
EVENT_SCHEMA_VERSION = "1.1"
try:  # pragma: no cover - best effort, fallback is fine
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from runtime.core.events import EVENT_LOG_SCHEMA_VERSION as EVENT_SCHEMA_VERSION  # noqa: F401
except Exception:
    pass


# The synthetic session the hero lesson traces back to. Appears in the
# lesson's frontmatter (session_id) and in the digest below, so
# `recall trace postgres-skip-locked-queue-claims` resolves the full chain:
# lesson -> provenance -> originating digest.
DEMO_SESSION_ID = "demo-2026-06-04-queue-double-claim"


# ---------------------------------------------------------------------------
# Synthetic lessons - frontmatter shape matches the auto-memory convention
# used by recall indexing and recall/remember.py (name / description / type /
# created + a short body with Why and How to apply). A lesson may carry
# `extra_fm` for provenance fields (the hero lesson uses the exact shape
# `recall remember --reviewed` writes).
# ---------------------------------------------------------------------------

LESSONS = [
    {
        "file": "feedback_quarantine_flaky_integration_tests.md",
        "name": "quarantine-flaky-integration-tests",
        "description": (
            "Quarantine flaky integration tests behind a marker the same day they "
            "first flake; never let them retrain the team to ignore red"
        ),
        "type": "feedback",
        "created": "2026-05-12T09:30:00+00:00",
        "body": (
            "When an integration test fails intermittently, tag it `@pytest.mark.flaky_quarantine`\n"
            "and exclude that marker from the merge-blocking CI job the same day.\n"
            "\n"
            "Why: one flaky test teaches the whole team to re-run CI instead of reading it.\n"
            "At Acme, Alice measured that a single 5%-flaky test added ~40 wasted CI hours a month.\n"
            "\n"
            "How to apply:\n"
            "- Move the test under the quarantine marker immediately; open a ticket with the failure seed.\n"
            "- Keep a nightly job that still runs the quarantined set so they cannot rot silently.\n"
            "- A test leaves quarantine only after 50 consecutive green nightly runs.\n"
        ),
    },
    {
        "file": "feedback_ci_dependency_caching.md",
        "name": "ci-dependency-caching",
        "description": (
            "Key CI dependency caches on the lockfile hash, not the branch name; "
            "a stale cache is slower than no cache"
        ),
        "type": "feedback",
        "created": "2026-05-18T14:05:00+00:00",
        "body": (
            "Cache package-manager downloads in CI with a key derived from the lockfile hash\n"
            "(`hashFiles('**/package-lock.json')` or the pip requirements files), plus a\n"
            "versioned prefix you can bump to invalidate everything at once.\n"
            "\n"
            "Why: branch-keyed caches miss on every new branch and silently grow stale on\n"
            "long-lived ones. Alice cut Acme's median CI run from 11 to 4 minutes by re-keying.\n"
            "\n"
            "How to apply:\n"
            "- Key: `v1-deps-<os>-<lockfile-hash>`; restore-keys fall back to `v1-deps-<os>-`.\n"
            "- Never cache the virtualenv itself across Python versions; cache the wheel/download dir.\n"
            "- Bump the `v1` prefix when the toolchain changes instead of debugging ghosts.\n"
        ),
    },
    {
        "file": "feedback_git_bisect_regression_hunting.md",
        "name": "git-bisect-regression-hunting",
        "description": (
            "Use git bisect run with a single-command repro script to find regressions; "
            "guessing from the diff wastes hours"
        ),
        "type": "feedback",
        "created": "2026-05-23T11:20:00+00:00",
        "body": (
            "When a regression appears and the offending change is not obvious, write the\n"
            "smallest script that exits 0 on good and 1 on bad, then let\n"
            "`git bisect run ./repro.sh` walk the history for you.\n"
            "\n"
            "Why: bisect is O(log n) and mechanical. Eyeballing a 60-commit range is O(pride)\n"
            "and error-prone. Alice found a 3-week-old regression at Acme in 7 bisect steps\n"
            "after two engineers had spent a day reading diffs.\n"
            "\n"
            "How to apply:\n"
            "- `git bisect start <bad> <good>`, then `git bisect run <script>`.\n"
            "- The repro script must be hermetic: build + run + assert, no manual steps.\n"
            "- Exit code 125 skips commits that do not build, keeping the search honest.\n"
        ),
    },
    {
        "file": "project_docker_compose_healthchecks.md",
        "name": "docker-compose-healthchecks",
        "description": (
            "Gate docker compose service startup on healthchecks with depends_on "
            "condition service_healthy, not sleep loops"
        ),
        "type": "project",
        "created": "2026-05-28T16:45:00+00:00",
        "body": (
            "In compose files, give every stateful service (postgres, redis, kafka) a\n"
            "`healthcheck` and make dependents use\n"
            "`depends_on: { db: { condition: service_healthy } }`.\n"
            "\n"
            "Why: `depends_on` without a condition only orders container *start*, not\n"
            "readiness. The Acme integration suite flaked for months on 'connection refused'\n"
            "until the sleep-based waits were replaced with real healthchecks.\n"
            "\n"
            "How to apply:\n"
            "- postgres: `test: [\"CMD-SHELL\", \"pg_isready -U $$POSTGRES_USER\"]`, interval 2s, retries 15.\n"
            "- Set `start_period` generously so slow cold starts do not count as failures.\n"
            "- In CI, `docker compose up --wait` returns non-zero if any healthcheck never passes.\n"
        ),
    },
    {
        "file": "feedback_code_review_checklist_habits.md",
        "name": "code-review-checklist-habits",
        "description": (
            "Review with a written checklist (tests, names, seams, rollback) instead of "
            "scrolling for vibes; checklists catch what attention misses"
        ),
        "type": "feedback",
        "created": "2026-06-02T08:10:00+00:00",
        "body": (
            "Keep a four-line personal checklist and walk it on every review, every time:\n"
            "1) Do the tests test the seam, not just the unit? 2) Do names tell the truth?\n"
            "3) What happens on partial failure? 4) How would we roll this back?\n"
            "\n"
            "Why: ad-hoc reviews catch style and miss contracts. After Alice's team at Acme\n"
            "adopted the checklist, the bugs that escaped to production shifted from\n"
            "'reviewer never looked there' to genuinely novel failure modes.\n"
            "\n"
            "How to apply:\n"
            "- Paste the checklist into the review description and tick items explicitly.\n"
            "- Anything you cannot tick becomes a comment, not a silent pass.\n"
            "- Re-read all user-facing strings out loud; wrong copy is a bug too.\n"
        ),
    },
    {
        # The demo's hero lesson: beat 1 auto-injects it, beat 2 traces it.
        # Its frontmatter is exactly what `recall remember --reviewed` writes
        # (source/created_by/provenance/reviewed_by) plus the session id, so
        # `recall trace` can walk it back to the digest below.
        "file": "postgres-skip-locked-queue-claims.md",
        "name": "postgres-skip-locked-queue-claims",
        "description": (
            "Claim queue jobs with SELECT FOR UPDATE SKIP LOCKED; plain row "
            "locks make workers double-claim or convoy behind each other"
        ),
        "type": "feedback",
        "created": "2026-06-04T15:12:00+00:00",
        "extra_fm": {
            "source": "recall-remember",
            "created_by": "recall-remember",
            "provenance": "human-cli",
            "session_id": DEMO_SESSION_ID,
            "reviewed_by": "human-cli",
        },
        "body": (
            "Workers claiming jobs from a Postgres-backed queue must select with\n"
            "`FOR UPDATE SKIP LOCKED`, not a bare `FOR UPDATE`.\n"
            "\n"
            "Why: with plain row locks, every idle worker queues behind the same hot row,\n"
            "and a retried transaction can hand the same job to two workers. Alice spent a\n"
            "day at Acme on 'duplicate welcome emails' that was exactly this.\n"
            "\n"
            "How to apply:\n"
            "- `SELECT ... FROM jobs WHERE status = 'ready' ORDER BY id\n"
            "  FOR UPDATE SKIP LOCKED LIMIT 1` inside the claiming transaction.\n"
            "- Mark the row taken in the same transaction; commit before starting the work.\n"
            "- A worker that dies mid-job releases the lock on rollback, so the job is\n"
            "  re-claimable with no janitor process.\n"
        ),
    },
]


# ---------------------------------------------------------------------------
# Synthetic staged candidates - shape matches what agent/tools/
# triage_candidates.py expects (status=staged, claim, cluster_size,
# canonical_salience, evidence_ids, staged_at, decisions, rejection_count).
# ---------------------------------------------------------------------------

def _candidates(now: datetime.datetime) -> list[dict]:
    day = datetime.timedelta(days=1)
    return [
        {
            "id": "cand_pin_compose_image_digests",
            "key": "cand_pin_compose_image_digests",
            "name": "cand_pin_compose_image_digests",
            "claim": (
                "Pin docker compose images to digests in CI; ':latest' broke the "
                "Acme integration suite twice this quarter"
            ),
            "conditions": [],
            "evidence_ids": ["digest-2026-06-03-ci-flake", "digest-2026-06-07-ci-flake"],
            "cluster_size": 2,
            "canonical_salience": 7.5,
            "staged_at": (now - 2 * day).isoformat(),
            "status": "staged",
            "decisions": [],
            "rejection_count": 0,
        },
        {
            "id": "cand_bisect_before_blame",
            "key": "cand_bisect_before_blame",
            "name": "cand_bisect_before_blame",
            "claim": (
                "Run git bisect with a scripted repro before assigning a regression "
                "to a teammate; Alice's last three 'obvious culprits' were innocent"
            ),
            "conditions": [],
            "evidence_ids": ["digest-2026-06-05-regression-hunt"],
            "cluster_size": 1,
            "canonical_salience": 6.0,
            "staged_at": (now - 1 * day).isoformat(),
            "status": "staged",
            "decisions": [],
            "rejection_count": 0,
        },
    ]


# ---------------------------------------------------------------------------
# Synthetic AutoRecall telemetry so `recall stats --since 7d` has data.
# Line shape matches runtime/core/events.py (required keys + x_* extensions).
# ---------------------------------------------------------------------------

def _auto_recall_events(now_ms: int) -> list[dict]:
    rng = random.Random(42)  # deterministic demo data
    hour = 3600 * 1000
    events: list[dict] = []

    def base(ts_ms: int) -> dict:
        return {
            "schema_version": EVENT_SCHEMA_VERSION,
            "ts_ms": ts_ms,
            "event": "AutoRecall",
            "session_id": "demo-session",
            "turn": 0,
        }

    # 9 hits spread over the past ~5 days
    for i in range(9):
        e = base(now_ms - (6 + i * 13) * hour)
        k = rng.choice([2, 3, 3])
        e.update(
            {
                "x_outcome": "hit",
                "x_latency_ms": rng.randint(180, 420),
                "x_k_returned": k,
                "x_sources": {"brain": k},
                "x_top_scores": [round(rng.uniform(0.55, 0.93), 2) for _ in range(k)],
            }
        )
        events.append(e)

    # 3 skips (prompt too short) + 1 timeout for realistic diagnostics
    for i in range(3):
        e = base(now_ms - (9 + i * 17) * hour)
        e.update({"x_outcome": "skip", "x_skip_reason": "too-short"})
        events.append(e)
    e = base(now_ms - 50 * hour)
    e.update({"x_outcome": "timeout"})
    events.append(e)

    events.sort(key=lambda ev: ev["ts_ms"])
    return events


def _pending_review_md(now: datetime.datetime, candidates: list[dict]) -> str:
    lines = [
        "# brainstack: pending review",
        "",
        f"_Generated {now.isoformat()}_",
        "",
        f"**{len(candidates)} candidates pending**",
        "",
        "## Candidates (default)",
    ]
    for c in candidates:
        lines.append(
            f"- `{c['id']}`: {c['claim'][:90]}... "
            f"(cluster {c['cluster_size']}, salience {c['canonical_salience']})"
        )
    lines += [
        "",
        "Run `recall pending --review` in your own terminal to triage "
        "(graduate / reject / skip: your keyboard, your call).",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "./demo/brain").resolve()
    now = datetime.datetime.now(datetime.timezone.utc)
    now_ms = int(now.timestamp() * 1000)

    lessons_dir = target / "memory" / "semantic" / "lessons"
    candidates_dir = target / "memory" / "candidates"
    imports_dir = target / "imports"
    logs_dir = target / "runtime" / "logs"
    for d in (lessons_dir, candidates_dir, imports_dir, logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    for lesson in LESSONS:
        fm_lines = [
            f"name: {lesson['name']}",
            f"description: {lesson['description']}",
            f"type: {lesson['type']}",
            f"created: {lesson['created']}",
        ]
        for key, value in lesson.get("extra_fm", {}).items():
            fm_lines.append(f"{key}: {value}")
        frontmatter = "---\n" + "\n".join(fm_lines) + "\n---\n\n"
        (lessons_dir / lesson["file"]).write_text(
            frontmatter + lesson["body"], encoding="utf-8"
        )

    # The digest the hero lesson's session_id resolves to. `recall trace`
    # scans memory/semantic/digests/ for the session id (filename or head)
    # and prints the match as "originating digest".
    digests_dir = target / "memory" / "semantic" / "digests"
    digests_dir.mkdir(parents=True, exist_ok=True)
    (digests_dir / f"2026-06-04__queue-double-claim-hunt__{DEMO_SESSION_ID}.md").write_text(
        "---\n"
        f'session_id: "{DEMO_SESSION_ID}"\n'
        "source: claude\n"
        "started_at: 2026-06-04T13:05:00+00:00\n"
        "ended_at: 2026-06-04T15:20:00+00:00\n"
        "domain_tags: [postgres, queues, debugging]\n"
        "outcome: completed\n"
        "---\n"
        "\n"
        "# Queue double-claim hunt\n"
        "\n"
        "## What you did\n"
        "\n"
        "Alice traced duplicate welcome emails at Acme to two workers claiming the\n"
        "same queue row. Reproduced with two psql sessions, fixed the claiming query\n"
        "with FOR UPDATE SKIP LOCKED, and remembered the lesson at the CLI.\n"
        "\n"
        "## What was learned\n"
        "\n"
        "Bare FOR UPDATE makes idle workers convoy behind the same hot row, and a\n"
        "retried transaction can hand one job to two workers. SKIP LOCKED gives each\n"
        "worker its own row and dead workers release claims on rollback.\n",
        encoding="utf-8",
    )

    cands = _candidates(now)
    for c in cands:
        (candidates_dir / f"{c['id']}.json").write_text(
            json.dumps(c, indent=2) + "\n", encoding="utf-8"
        )

    # One synthetic doc in the imports tier. The auto-generated default
    # config indexes $BRAIN_ROOT/imports as a second source, and embedded
    # qdrant's hybrid query raises KeyError('sparse') on a collection with
    # zero points, so the tier must not be empty.
    (imports_dir / "acme_oncall_handoff_notes.md").write_text(
        "---\n"
        "name: acme-oncall-handoff-notes\n"
        "description: Synthetic imported note about Acme on-call handoff habits\n"
        "type: reference\n"
        "created: 2026-06-01T10:00:00+00:00\n"
        "---\n"
        "\n"
        "Handoff template Alice uses at Acme: open incidents with links, silenced\n"
        "alerts with expiry dates, and any deploy freezes. Five minutes of writing\n"
        "saves the next on-call an hour of archaeology.\n",
        encoding="utf-8",
    )

    (target / "PENDING_REVIEW.md").write_text(
        _pending_review_md(now, cands), encoding="utf-8"
    )

    # Runtime config for the hook beat and `recall stats`: auto-recall is
    # enabled with a generous timeout (the recording invokes the hook in a
    # fresh process, so the dense model loads from disk inside the budget;
    # the production default stays 3000ms). The events log lives inside the
    # demo brain so a recording never reads the recording machine's real
    # telemetry. Absolute path because RuntimeConfig only expanduser()s.
    (target / "runtime.toml").write_text(
        "# Synthetic runtime config for the demo. Use via:\n"
        '#   export RECALL_RUNTIME_CONFIG="$PWD/demo/brain/runtime.toml"\n'
        "[tool.recall.runtime]\n"
        f'log_dir = "{logs_dir}"\n'
        "enable_auto_recall = true\n"
        "auto_recall_timeout_ms = 20000\n"
        "auto_recall_k = 3\n",
        encoding="utf-8",
    )

    with (logs_dir / "events.log.jsonl").open("w", encoding="utf-8") as f:
        for ev in _auto_recall_events(now_ms):
            f.write(json.dumps(ev, sort_keys=True, separators=(",", ":")) + "\n")

    print(f"demo brain written to {target}")
    print(f"  lessons:    {len(LESSONS)} in {lessons_dir}")
    print(f"  digests:    1 in {digests_dir}")
    print(f"  candidates: {len(cands)} staged in {candidates_dir}")
    print(f"  telemetry:  {logs_dir / 'events.log.jsonl'}")
    print('next: export BRAIN_ROOT="' + str(target) + '"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
