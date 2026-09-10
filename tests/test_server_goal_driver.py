from __future__ import annotations

import importlib.util
import json
import subprocess
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


BUS = load("chatgpt_server_bus", "chatgpt_server_bus.py")
AGY = load("agy_server_worker", "agy_server_worker.py")
DRIVER = load("server_goal_driver", "server_goal_driver.py")


def create_goal(db: Path) -> None:
    BUS.create_goal(
        db, goal_id="g1", owner="gemini", created_by="user", description="Ship safely.",
    )


def test_waits_while_assigned_work_is_outstanding_without_calling_model(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"
    create_goal(db)
    BUS.add_goal_task(
        db, goal_id="g1", task_id="t1", assignee="codex", repo_id="automation",
        created_by="gemini", description="Implement the change.",
    )
    called = False

    def execute(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("model must not run")

    result = DRIVER.advance(db_path=db, goal_id="g1", execute=execute)
    assert result["action"] == "wait"
    assert result["reason"] == "assigned_work_outstanding"
    assert called is False


def test_provider_lock_contention_is_wait_not_failure(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"
    create_goal(db)
    lock = tmp_path / "provider.lock"
    with BUS.provider_slot(lock) as acquired:
        assert acquired is True
        result = DRIVER.advance(db_path=db, goal_id="g1", provider_lock=lock)
    assert result == {"action": "wait", "reason": "provider_busy", "goal_id": "g1"}
    assert BUS.goal_driver_status(db, goal_id="g1")["runs"] == []


def test_gemini_adds_one_task_and_driver_records_the_turn(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"
    create_goal(db)

    def execute(argv, **kwargs):
        assert kwargs["env"]["PYTHONUTF8"] == "1"
        if Path("/snap/bin").is_dir():
            assert kwargs["env"]["PATH"].split(":", 1)[0] == "/snap/bin"
        return subprocess.CompletedProcess(
            argv, 0,
            stdout=json.dumps({
                "action": "add_task", "task_id": "inspect", "assignee": "chatgpt",
                "repo_id": "automation",
                "description": "Inspect current implementation and report concrete defects.",
            }),
            stderr="",
        )

    result = DRIVER.advance(db_path=db, goal_id="g1", execute=execute)
    assert result["action"] == "goal_task_transition"
    assert result["task_id"] == "inspect"
    state = BUS.goal_status(db, goal_id="g1")
    assert state["tasks"][0]["status"] == "open"
    runs = BUS.goal_driver_status(db, goal_id="g1")["runs"]
    assert len(runs) == 1 and runs[0]["status"] == "completed"
    assert "prompt" not in json.dumps(runs)


def test_manager_prompt_routes_chatgpt_only_to_ordinary_web_chat() -> None:
    prompt = DRIVER.build_prompt(
        {"goal": {"goal_id": "g1"}, "summary": {"total": 0}, "tasks": []},
        assignees=DRIVER.DEFAULT_ASSIGNEES,
        repo_ids=("automation", "stock"),
    )
    assert "ordinary ChatGPT web chat/browser session only" in prompt
    assert "never select or route implementation through ChatGPT Worker" in prompt
    assert "never as a substitute implementation path" in prompt


def test_manager_add_task_requires_registered_repo_id(tmp_path: Path) -> None:
    material = {
        "goal": {"goal_id": "g1"},
        "summary": {"total": 0, "completed": 0},
        "tasks": [],
    }
    with pytest.raises(DRIVER.GoalDriverError) as failure:
        DRIVER.parse_decision(
            json.dumps({
                "action": "add_task", "task_id": "inspect", "assignee": "chatgpt",
                "repo_id": "invented", "description": "Inspect the repo.",
            }),
            material=material, assignees=DRIVER.DEFAULT_ASSIGNEES,
            repo_ids=("automation", "stock"),
        )
    assert failure.value.code == "MANAGER_DECISION_INVALID"


def test_manager_cannot_self_assign_durable_execution_task() -> None:
    material = {
        "goal": {"goal_id": "g1"},
        "summary": {"total": 0, "completed": 0},
        "tasks": [],
    }
    with pytest.raises(DRIVER.GoalDriverError) as failure:
        DRIVER.parse_decision(
            json.dumps({
                "action": "add_task", "task_id": "inspect", "assignee": "gemini",
                "repo_id": "automation", "description": "Inspect repository state.",
            }),
            material=material, assignees=DRIVER.DEFAULT_ASSIGNEES,
            repo_ids=DRIVER.DEFAULT_REPO_IDS,
        )
    assert failure.value.code == "MANAGER_SELF_ASSIGNMENT_FORBIDDEN"


def test_invalid_manager_attempt_is_attention_required_and_never_auto_replayed(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"
    create_goal(db)
    calls = 0

    def bad_execute(argv, **kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            argv, 0,
            stdout=json.dumps({
                "action": "transition_task", "task_id": "missing", "status": "completed",
            }),
            stderr="",
        )

    first = DRIVER.advance(db_path=db, goal_id="g1", execute=bad_execute)
    assert first["action"] == "goal_driver_attention"
    assert first["automatic_retry"] is False
    second = DRIVER.advance(db_path=db, goal_id="g1", execute=bad_execute)
    assert second["action"] == "attention_required"
    assert calls == 1

    note = BUS.acknowledge_goal_driver_run(
        db, run_id=first["run_id"], changed_by="user", note="Reviewed; safe to ask again.",
    )
    assert note["status"] == "acknowledged"


def test_timeout_is_attention_required_and_preserves_no_retry_boundary(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"
    create_goal(db)

    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, timeout=1)

    result = DRIVER.advance(db_path=db, goal_id="g1", execute=timeout, process_timeout=1)
    assert result["action"] == "goal_driver_attention"
    assert result["error_code"] == "MODEL_TIMEOUT"
    assert result["automatic_retry"] is False
    runs = BUS.goal_driver_status(db, goal_id="g1")["runs"]
    assert runs[0]["status"] == "attention_required"


def test_manager_never_records_task_completion(tmp_path: Path) -> None:
    material = {
        "goal": {"goal_id": "g1"},
        "summary": {"total": 1, "completed": 0},
        "tasks": [{"task_id": "t1", "status": "in_progress"}],
    }
    with pytest.raises(DRIVER.GoalDriverError) as failure:
        DRIVER.parse_decision(
            json.dumps({"action": "transition_task", "task_id": "t1", "status": "completed"}),
            material=material,
            assignees=DRIVER.DEFAULT_ASSIGNEES,
            repo_ids=DRIVER.DEFAULT_REPO_IDS,
        )
    assert failure.value.code == "MANAGER_TASK_COMPLETION_FORBIDDEN"


def test_goal_completion_requires_existing_explicitly_completed_task(tmp_path: Path) -> None:
    material = {
        "goal": {"goal_id": "g1"},
        "summary": {"total": 0, "completed": 0},
        "tasks": [],
    }
    with pytest.raises(DRIVER.GoalDriverError) as failure:
        DRIVER.parse_decision(
            json.dumps({"action": "transition_goal", "status": "completed"}),
            material=material,
            assignees=DRIVER.DEFAULT_ASSIGNEES,
            repo_ids=DRIVER.DEFAULT_REPO_IDS,
        )
    assert failure.value.code == "MANAGER_GOAL_COMPLETION_UNPROVEN"


def test_smoke_resume_across_reopen_then_complete_goal(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"
    create_goal(db)

    def add_task(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 0,
            stdout='{"action":"add_task","task_id":"day1","assignee":"chatgpt","repo_id":"automation","description":"Day one work."}',
            stderr="",
        )

    DRIVER.advance(db_path=db, goal_id="g1", execute=add_task)
    BUS.transition_goal_task(
        db, goal_id="g1", task_id="day1", status="in_progress", changed_by="chatgpt",
    )
    reopened = load("server_goal_driver_bus_reopened", "chatgpt_server_bus.py")
    assert reopened.goal_status(db, goal_id="g1")["tasks"][0]["status"] == "in_progress"
    reopened.transition_goal_task(
        db, goal_id="g1", task_id="day1", status="completed", changed_by="chatgpt",
    )

    def finish(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 0,
            stdout='{"action":"transition_goal","status":"completed"}', stderr="",
        )

    result = DRIVER.advance(db_path=db, goal_id="g1", execute=finish)
    assert result["action"] == "goal_transition"
    assert result["to_status"] == "completed"
    assert reopened.goal_status(db, goal_id="g1")["goal"]["status"] == "completed"


def test_advance_all_skips_assigned_work_and_advances_only_one_ready_goal(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"
    create_goal(db)
    BUS.add_goal_task(
        db, goal_id="g1", task_id="busy", assignee="codex", repo_id="automation",
        created_by="gemini", description="Already assigned work.",
    )
    BUS.create_goal(
        db, goal_id="g2", owner="gemini", created_by="user", description="Second goal.",
    )
    calls = 0

    def execute(argv, **kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            argv, 0,
            stdout='{"action":"add_task","task_id":"next","assignee":"claude","repo_id":"automation","description":"Do one bounded task."}',
            stderr="",
        )

    first = DRIVER.advance_all(db_path=db, execute=execute)
    assert first["goal_id"] == "g2"
    assert first["task_id"] == "next"
    assert calls == 1

    second = DRIVER.advance_all(db_path=db, execute=execute)
    assert second["action"] == "wait"
    assert second["reason"] == "no_manager_ready_goals"
    assert calls == 1


def test_advance_all_reports_stuck_manager_without_replaying_when_no_other_goal_is_ready(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"
    create_goal(db)
    material = DRIVER.goal_material(db, goal_id="g1")
    reserved = BUS.reserve_goal_driver_run(
        db, goal_id="g1", manager="gemini",
        snapshot_sha256=DRIVER.snapshot_sha256(material), prompt="private manager prompt",
    )
    attention = BUS.attention_goal_driver_run(
        db, run_id=reserved["run_id"], manager="gemini",
        error_code="MODEL_TIMEOUT", detail="private timeout detail",
    )
    assert attention["status"] == "attention_required"

    called = False

    def execute(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("stuck goal must not replay")

    result = DRIVER.advance_all(db_path=db, execute=execute)
    assert result["action"] == "goal_driver_attention"
    assert result["goal_id"] == "g1"
    assert result["run_id"] == reserved["run_id"]
    assert result["error_code"] == "MODEL_TIMEOUT"
    assert result["automatic_retry"] is False
    assert called is False


def test_cli_advance_requires_exactly_one_goal_selector(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"
    create_goal(db)
    parser = DRIVER.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--db", str(db), "advance"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--db", str(db), "advance", "--goal-id", "g1", "--all"])
