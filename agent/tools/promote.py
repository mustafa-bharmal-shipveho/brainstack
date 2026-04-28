"""Promote a counterparty/class to tier 2 in the namespace policy.

CLI:
  promote.py --namespace NS --target SENDER_OR_CLASS --tier 2 \
      --reason "..." [--reviewer NAME]

The policy.yaml file (or policy.json fallback) for the namespace gets a
tiers entry: tiers[target] = {tier, since, reason, reviewer}. An audit
record is appended to memory/audit/<ns>/promotions.jsonl.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys


BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
# SDK lives in agent/memory/.
sys.path.insert(0, os.path.join(BASE, "memory"))


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


def _append_audit(path, entry):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def main():
    p = argparse.ArgumentParser(description="Promote target to tier in policy.")
    p.add_argument("--namespace", required=True)
    p.add_argument("--target", required=True,
                   help="Sender / class identifier to promote.")
    p.add_argument("--tier", type=int, default=2)
    p.add_argument("--reason", required=True)
    p.add_argument("--reviewer", default="host-agent")
    args = p.parse_args()

    import sdk  # type: ignore[import-not-found]

    brain_root = _resolve_brain_root()
    policy = sdk.read_policy(args.namespace, brain_root=brain_root)
    tiers = policy.setdefault("tiers", {})
    tiers[args.target] = {
        "tier": int(args.tier),
        "since": _now_iso(),
        "reason": args.reason,
        "reviewer": args.reviewer,
    }
    sdk.write_policy(args.namespace, policy, brain_root=brain_root)

    _append_audit(_audit_path(brain_root, args.namespace), {
        "ts": _now_iso(),
        "action": "promote",
        "target": args.target,
        "tier": int(args.tier),
        "reason": args.reason,
        "reviewer": args.reviewer,
        "namespace": args.namespace,
    })

    print(f"promoted {args.target} → tier {args.tier} in namespace {args.namespace}")


if __name__ == "__main__":
    main()
