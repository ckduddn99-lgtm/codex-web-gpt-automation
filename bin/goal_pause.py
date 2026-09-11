#!/usr/bin/env python3
"""Pause one durable /goal after an explicit user request."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import chatgpt_server_bus as BUS


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--goal-id", required=True)
    args = parser.parse_args()
    state = BUS.goal_status(args.db, goal_id=args.goal_id)
    current = str(state["goal"]["status"])
    if current in {"blocked", "user_decision_required", "completed"}:
        print(json.dumps({"action": "goal_pause", "goal_id": args.goal_id, "status": current, "changed": False}))
        return 0
    result = BUS.transition_goal(
        args.db,
        goal_id=args.goal_id,
        status="blocked",
        changed_by="user",
        blocker="Paused by explicit user request.",
    )
    print(json.dumps({"action": "goal_pause", "goal_id": args.goal_id, "status": "blocked", "changed": True, "transition": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
