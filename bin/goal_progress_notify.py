#!/usr/bin/env python3
"""Emit one safe Discord heartbeat for currently active durable /goal work."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


GOAL = _load("goal_progress_notify_driver", "server_goal_driver.py")
NOTIFY = _load("goal_progress_notify_board", "board_notify.py")
DEFAULT_DB = Path(os.environ.get("PROJECT_CONTROL_DB", str(Path.home() / ".local/state/ai-bus/bus.sqlite3")))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--channel", default="일반")
    args = parser.parse_args()
    payload = GOAL.next_ready_goal(args.db)
    if payload.get("action") != "goal_progress":
        print(json.dumps({"sent": False, "reason": "no_active_goal_progress", "payload": payload}, ensure_ascii=False))
        return 0
    result = NOTIFY.notify(payload, channel=args.channel)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
