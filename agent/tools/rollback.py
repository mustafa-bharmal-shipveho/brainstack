"""Rollback a tier promotion. Drops to tier 1 and stamps a 30-day cooldown.

CLI:
  rollback.py --namespace NS --target SENDER_OR_CLASS \
      --reason "..." [--reviewer NAME]

The policy.yaml entry for the target is rewritten with tier=1 and
cooldown_until=now+30d. Audit record appended to
memory/audit/<ns>/promotions.jsonl with action=rollback.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys


BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(BASE, "memory"))


COOLDOWN_DAYS = 30


def _resolve_brain_root():
    env = os.environ.get("BRAIN_ROOT")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return BASE


def _audit_path(brain_root, namespace):
    return os.path.join(brain_root, "memory", "audit", namespace,
                        "promotions.jsonl")


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _cooldown_iso():
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(days=COOLDOWN_DAYS)).isoformat()


def _append_audit(path, entry):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def main():
    p = argparse.ArgumentParser(description="Rollback a tier promotion.")
    p.add_argument("--namespace", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--reviewer", default="host-agent")
    args = p.parse_args()

    import sdk  # type: ignore[import-not-found]

    brain_root = _resolve_brain_root()
    policy = sdk.read_policy(args.namespace, brain_root=brain_root)
    tiers = policy.setdefault("tiers", {})
    tiers[args.target] = {
        "tier": 1,
        "since": _now_iso(),
        "reason": args.reason,
        "reviewer": args.reviewer,
        "cooldown_until": _cooldown_iso(),
    }
    sdk.write_policy(args.namespace, policy, brain_root=brain_root)

    _append_audit(_audit_path(brain_root, args.namespace), {
        "ts": _now_iso(),
        "action": "rollback",
        "target": args.target,
        "tier": 1,
        "cooldown_until": tiers[args.target]["cooldown_until"],
        "reason": args.reason,
        "reviewer": args.reviewer,
        "namespace": args.namespace,
    })

    print(f"rolled back {args.target} to tier 1 in namespace {args.namespace} "
          f"(cooldown until {tiers[args.target]['cooldown_until']})")


if __name__ == "__main__":
    main()
