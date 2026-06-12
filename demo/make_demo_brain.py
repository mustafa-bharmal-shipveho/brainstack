#!/usr/bin/env python3
"""Build a fully synthetic demo brain for the recorded demo (demo/demo.tape).

Usage:
    python3 demo/make_demo_brain.py [TARGET_DIR]    # default: ./demo/brain

Creates:
    TARGET_DIR/
      memory/semantic/lessons/*.md     5 synthetic lessons (real frontmatter shape)
      memory/candidates/*.json         2 staged dream candidates (real triage schema)
      imports/*.md                     1 synthetic imported note (the default config
                                       indexes an imports tier; an empty collection
                                       crashes embedded-qdrant hybrid queries)
      PENDING_REVIEW.md                summary consumed by `recall pending`
      runtime.toml                     runtime config pointing stats at the demo log
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


# ---------------------------------------------------------------------------
# Synthetic lessons - frontmatter shape matches the auto-memory convention
# used by recall indexing and recall/remember.py (name / description / type /
# created + a short body with Why and How to apply).
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
        frontmatter = (
            "---\n"
            f"name: {lesson['name']}\n"
            f"description: {lesson['description']}\n"
            f"type: {lesson['type']}\n"
            f"created: {lesson['created']}\n"
            "---\n\n"
        )
        (lessons_dir / lesson["file"]).write_text(
            frontmatter + lesson["body"], encoding="utf-8"
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

    # Runtime config for `recall stats`: point the events log inside the
    # demo brain so a recording never reads the recording machine's real
    # telemetry. Absolute path because RuntimeConfig only expanduser()s.
    (target / "runtime.toml").write_text(
        "# Synthetic runtime config for the demo. Use via:\n"
        '#   export RECALL_RUNTIME_CONFIG="$PWD/demo/brain/runtime.toml"\n'
        "[tool.recall.runtime]\n"
        f'log_dir = "{logs_dir}"\n',
        encoding="utf-8",
    )

    with (logs_dir / "events.log.jsonl").open("w", encoding="utf-8") as f:
        for ev in _auto_recall_events(now_ms):
            f.write(json.dumps(ev, sort_keys=True, separators=(",", ":")) + "\n")

    print(f"demo brain written to {target}")
    print(f"  lessons:    {len(LESSONS)} in {lessons_dir}")
    print(f"  candidates: {len(cands)} staged in {candidates_dir}")
    print(f"  telemetry:  {logs_dir / 'events.log.jsonl'}")
    print('next: export BRAIN_ROOT="' + str(target) + '"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
