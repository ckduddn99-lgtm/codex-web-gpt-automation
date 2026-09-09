from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


BIN = Path(__file__).resolve().parents[1] / "bin"
spec = importlib.util.spec_from_file_location("chatgpt_server_bus", BIN / "chatgpt_server_bus.py")
assert spec and spec.loader
BUS = importlib.util.module_from_spec(spec)
sys.modules.setdefault("chatgpt_server_bus", BUS)
spec.loader.exec_module(BUS)


def _goal(db: Path, *, assignee: str = "codex") -> None:
    BUS.create_goal(db, goal_id="goal-1", owner="gemini", created_by="user",
                    description="Build and verify the requested change.")
    BUS.add_goal_task(db, goal_id="goal-1", task_id="task-1", assignee=assignee,
                      repo_id="automation", created_by="gemini",
                      description="Implement the smallest safe change.")


def test_claim_reserves_before_provider_work(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"
    _goal(db)
    run = BUS.claim_goal_task(db, assignee="codex", worker_id="goal-codex")
    assert run is not None and run["automatic_retry"] is False
    assert BUS.goal_status(db, goal_id="goal-1")["tasks"][0]["status"] == "in_progress"
    assert BUS.claim_goal_task(db, assignee="codex", worker_id="other-worker") is None
    material = BUS.goal_task_input(db, run_id=run["run_id"], assignee="codex",
                                   lease_token=run["lease_token"])
    assert material["task"]["body"] == "Implement the smallest safe change."
    assert material["task"]["repo_id"] == "automation"


def test_success_explicitly_completes_task(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"
    _goal(db)
    run = BUS.claim_goal_task(db, assignee="codex", worker_id="goal-codex")
    assert run is not None
    settled = BUS.complete_goal_task_run(
        db, run_id=run["run_id"], assignee="codex", lease_token=run["lease_token"],
        result_status="completed", result="Implemented and tests passed.")
    assert settled["result_status"] == "completed"
    assert BUS.goal_status(db, goal_id="goal-1")["tasks"][0]["status"] == "completed"
    assert BUS.goal_task_run_status(db, goal_id="goal-1")["runs"][-1]["status"] == "completed"


def test_attention_never_requeues_or_infers_completion(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"
    _goal(db)
    run = BUS.claim_goal_task(db, assignee="codex", worker_id="goal-codex")
    assert run is not None
    attention = BUS.attention_goal_task_run(
        db, run_id=run["run_id"], assignee="codex", lease_token=run["lease_token"],
        error_code="MODEL_TIMEOUT", detail="Provider may have executed before timeout.")
    assert attention["automatic_retry"] is False
    assert BUS.goal_status(db, goal_id="goal-1")["tasks"][0]["status"] == "in_progress"
    assert BUS.claim_goal_task(db, assignee="codex", worker_id="other-worker") is None
    ack = BUS.acknowledge_goal_task_run(
        db, run_id=run["run_id"], changed_by="user",
        note="Reviewed the uncertain run; safe to retry.", requeue=True,
        reassign_to="chatgpt")
    assert ack["requeued"] is True
    assert ack["reassigned_to"] == "chatgpt"
    task = BUS.goal_status(db, goal_id="goal-1")["tasks"][0]
    assert task["status"] == "open"
    assert task["assignee"] == "chatgpt"


def test_blocked_result_requires_blocker(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"
    _goal(db, assignee="claude")
    run = BUS.claim_goal_task(db, assignee="claude", worker_id="goal-claude")
    assert run is not None
    with pytest.raises(BUS.BusError) as exc:
        BUS.complete_goal_task_run(db, run_id=run["run_id"], assignee="claude",
                                   lease_token=run["lease_token"],
                                   result_status="blocked", result="Cannot continue.")
    assert exc.value.code == "BLOCKER_REQUIRED"
    settled = BUS.complete_goal_task_run(
        db, run_id=run["run_id"], assignee="claude", lease_token=run["lease_token"],
        result_status="blocked", result="Cannot continue.", blocker="Missing required evidence.")
    assert settled["result_status"] == "blocked"
