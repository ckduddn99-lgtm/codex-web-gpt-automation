#!/usr/bin/env python3
"""Policy-limited project control plane over the durable goal backlog."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


BUS = _load("project_control_bus", "chatgpt_server_bus.py")
GOAL = _load("project_control_goal", "server_goal_driver.py")
WORKER = _load("project_control_worker", "server_goal_task_worker.py")
REGISTRY = _load("project_control_registry", "project_repo_registry.py")
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path.home() / ".local/state/ai-bus/bus.sqlite3"


class ProjectControlError(RuntimeError):
    pass


def _repo_id(value: str) -> str:
    try:
        return REGISTRY.normalize_repo_id(value)
    except REGISTRY.RepoRegistryError as exc:
        raise ProjectControlError(str(exc)) from exc


def load_repo_registry(items: Sequence[str] = ()) -> dict[str, Path]:
    try:
        return REGISTRY.load_registry(items)
    except REGISTRY.RepoRegistryError as exc:
        raise ProjectControlError(str(exc)) from exc


def repo_status(registry: Mapping[str, Path]) -> dict[str, Any]:
    return {
        "action": "project_repos",
        "repos": [
            {"repo_id": name, "available": Path(path).is_dir(), "name": Path(path).name}
            for name, path in sorted(registry.items())
        ],
    }


def create_goal(db: Path, *, goal_id: str, description: str) -> dict[str, Any]:
    return BUS.create_goal(
        db, goal_id=goal_id, owner="gemini", created_by="user", description=description,
    )


def add_task(
    db: Path, *, registry: Mapping[str, Path], goal_id: str, task_id: str,
    assignee: str, repo_id: str, description: str,
) -> dict[str, Any]:
    repo_id = _repo_id(repo_id)
    if repo_id not in registry:
        raise ProjectControlError(f"repository id {repo_id!r} is not registered")
    return BUS.add_goal_task(
        db, goal_id=goal_id, task_id=task_id, assignee=assignee, repo_id=repo_id,
        created_by="user", description=description,
    )


def tick(
    db: Path, *, registry: Mapping[str, Path], provider_lock: Path | None = None,
) -> dict[str, Any]:
    task = WORKER.run_one(
        db_path=db, repo=registry.get("automation", REPO_ROOT), repo_routes=registry,
        provider_lock=provider_lock,
    )
    manager = GOAL.advance_all(
        db_path=db, repo_ids=tuple(registry), provider_lock=provider_lock,
    )
    return {"action": "project_tick", "goal_task": task, "goal_manager": manager}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--repo-route", action="append", default=[], metavar="ID=PATH")
    parser.add_argument("--provider-lock", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("repos")
    commands.add_parser("backlog")
    status = commands.add_parser("goal-status"); status.add_argument("--goal-id", required=True)
    goal = commands.add_parser("create-goal")
    goal.add_argument("--goal-id", required=True); goal.add_argument("--description", required=True)
    task = commands.add_parser("add-task")
    task.add_argument("--goal-id", required=True); task.add_argument("--task-id", required=True)
    task.add_argument("--assignee", required=True); task.add_argument("--repo-id", required=True)
    task.add_argument("--description", required=True)
    commands.add_parser("tick")
    return parser


def main(argv: Sequence[str] | None = None, *, output=print) -> int:
    args = build_parser().parse_args(argv)
    try:
        registry = load_repo_registry(args.repo_route)
        if args.command == "repos": payload = repo_status(registry)
        elif args.command == "backlog": payload = BUS.backlog_summary(args.db)
        elif args.command == "goal-status": payload = BUS.goal_status(args.db, goal_id=args.goal_id)
        elif args.command == "create-goal": payload = create_goal(args.db, goal_id=args.goal_id, description=args.description)
        elif args.command == "add-task": payload = add_task(
            args.db, registry=registry, goal_id=args.goal_id, task_id=args.task_id,
            assignee=args.assignee, repo_id=args.repo_id, description=args.description,
        )
        else: payload = tick(args.db, registry=registry, provider_lock=args.provider_lock)
    except (ProjectControlError, BUS.BusError, GOAL.GoalDriverError) as exc:
        output(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    output(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
