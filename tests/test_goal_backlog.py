from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest


BIN = Path(__file__).resolve().parents[1] / "bin"


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, BIN / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


BUS = load("goal_backlog_bus", "chatgpt_server_bus.py")
NOTIFY = load("goal_backlog_notify", "board_notify.py")


def test_goal_and_tasks_persist_with_explicit_status_and_summary(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"
    BUS.create_goal(
        db, goal_id="ship-v2", owner="gemini", created_by="gemini",
        description="Ship the durable goal backlog.",
    )
    BUS.add_goal_task(
        db, goal_id="ship-v2", task_id="schema", assignee="codex",
        repo_id="automation", created_by="gemini", description="Add persistent tables.",
    )
    BUS.add_goal_task(
        db, goal_id="ship-v2", task_id="ux", assignee="chatgpt",
        repo_id="automation", created_by="gemini", description="Review operator output.",
    )

    BUS.transition_goal_task(
        db, goal_id="ship-v2", task_id="schema", status="completed",
        changed_by="codex",
    )
    blocked = BUS.transition_goal_task(
        db, goal_id="ship-v2", task_id="ux", status="blocked",
        changed_by="chatgpt", blocker="Need an operator decision.",
    )

    assert blocked["from_status"] == "open"
    assert blocked["to_status"] == "blocked"
    assert "blocker" not in blocked
    state = BUS.goal_status(db, goal_id="ship-v2")
    assert state["summary"]["completed"] == 1
    assert state["summary"]["blocked"] == 1
    assert state["summary"]["user_decision_required"] == 0
    assert {row["assignee"] for row in state["tasks"]} == {"codex", "chatgpt"}
    assert all("body" not in row for row in state["tasks"])

    summary = BUS.backlog_summary(db)
    assert summary["summary"] == {
        "completed": 1, "blocked": 1, "user_decision_required": 0,
    }


def test_blocked_and_user_decision_require_explicit_reason(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"
    BUS.create_goal(
        db, goal_id="g1", owner="gemini", created_by="gemini", description="Goal",
    )
    BUS.add_goal_task(
        db, goal_id="g1", task_id="t1", assignee="codex",
        repo_id="automation", created_by="gemini", description="Task",
    )

    for status in ("blocked", "user_decision_required"):
        with pytest.raises(BUS.BusError) as failure:
            BUS.transition_goal_task(
                db, goal_id="g1", task_id="t1", status=status, changed_by="codex",
            )
        assert failure.value.code == "BLOCKER_REQUIRED"


def test_goal_completion_never_infers_unfinished_child_completion(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"
    BUS.create_goal(
        db, goal_id="g1", owner="gemini", created_by="gemini", description="Goal",
    )
    BUS.add_goal_task(
        db, goal_id="g1", task_id="t1", assignee="codex",
        repo_id="automation", created_by="gemini", description="Task",
    )

    with pytest.raises(BUS.BusError) as failure:
        BUS.transition_goal(db, goal_id="g1", status="completed", changed_by="gemini")
    assert failure.value.code == "GOAL_TASKS_INCOMPLETE"

    BUS.transition_goal_task(
        db, goal_id="g1", task_id="t1", status="completed", changed_by="codex",
    )
    done = BUS.transition_goal(db, goal_id="g1", status="completed", changed_by="gemini")
    assert done["to_status"] == "completed"


def test_backlog_survives_reopen_and_preserves_transition_history(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"
    BUS.create_goal(
        db, goal_id="multi-day", owner="gemini", created_by="gemini",
        description="Resume this tomorrow.",
    )
    BUS.add_goal_task(
        db, goal_id="multi-day", task_id="day1", assignee="claude",
        repo_id="automation", created_by="gemini", description="First day work.",
    )
    BUS.transition_goal_task(
        db, goal_id="multi-day", task_id="day1", status="in_progress",
        changed_by="claude",
    )

    reloaded = load("goal_backlog_bus_reloaded", "chatgpt_server_bus.py")
    state = reloaded.goal_status(db, goal_id="multi-day")
    assert state["tasks"][0]["status"] == "in_progress"
    assert [row["to_status"] for row in state["transitions"]] == ["open", "open", "in_progress"]


def test_goal_artifact_is_scoped_and_not_in_status_payload(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"
    BUS.create_goal(
        db, goal_id="g1", owner="gemini", created_by="gemini",
        description="private goal body",
    )
    state = BUS.goal_status(db, goal_id="g1")
    ref = state["goal"]["description_ref"]
    assert "private goal body" not in json.dumps(state)
    assert BUS.goal_artifact(db, goal_id="g1", ref=ref)["body"] == "private goal body"


def test_discord_backlog_event_contains_only_transition_metadata(tmp_path: Path) -> None:
    payload = {
        "action": "goal_task_transition", "goal_id": "g1", "task_id": "t1",
        "from_status": "in_progress", "to_status": "blocked",
        "assignee": "codex", "changed_by": "gemini",
        "blocker": "SECRET BLOCKER BODY", "prompt": "SECRET PROMPT",
    }
    result = NOTIFY.notify(payload, state_path=tmp_path / "notify.json", dry_run=True)
    assert result["reason"] == "dry_run"
    assert "in_progress" in result["message"] and "blocked" in result["message"]
    assert "SECRET BLOCKER BODY" not in result["message"]
    assert "SECRET PROMPT" not in result["message"]


def test_existing_goal_tasks_migrate_without_guessing_a_repository(tmp_path: Path) -> None:
    db = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db) as raw:
        raw.execute("""CREATE TABLE goal_tasks (
            goal_id TEXT NOT NULL, task_id TEXT NOT NULL, description_ref INTEGER NOT NULL,
            assignee TEXT NOT NULL, status TEXT NOT NULL, blocker_ref INTEGER,
            source_round_id TEXT, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, completed_at TEXT, PRIMARY KEY (goal_id, task_id)
        )""")
        raw.execute(
            """INSERT INTO goal_tasks VALUES
               ('g', 't', 1, 'codex', 'blocked', NULL, NULL, 'gemini', 'a', 'b', NULL)"""
        )
    BUS.initialize(db)
    with BUS.connect(db) as migrated:
        row = migrated.execute(
            "SELECT repo_id FROM goal_tasks WHERE goal_id = 'g' AND task_id = 't'"
        ).fetchone()
    assert row["repo_id"] == BUS.LEGACY_REPO_ID


def test_cli_roundtrip_for_backlog(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"
    description = tmp_path / "goal.md"
    description.write_text("장기 목표", encoding="utf-8")
    lines: list[str] = []
    assert BUS.main([
        "--db", str(db), "create-goal", "--goal-id", "g1", "--owner", "gemini",
        "--created-by", "gemini", "--description-file", str(description),
    ], output=lines.append) == 0
    created = json.loads(lines[-1])
    assert created["to_status"] == "open"

    lines.clear()
    assert BUS.main(["--db", str(db), "backlog-summary"], output=lines.append) == 0
    assert json.loads(lines[-1])["summary"]["completed"] == 0
