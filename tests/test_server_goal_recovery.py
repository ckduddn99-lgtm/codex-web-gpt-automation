from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))


def load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, BIN / file)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


BUS = load("chatgpt_server_bus", "chatgpt_server_bus.py")
RECOVERY = load("server_goal_recovery", "server_goal_recovery.py")


def _attention(db: Path, *, assignee: str, code: str, detail: str) -> int:
    BUS.create_goal(db, goal_id="g", owner="gemini", created_by="user", description="Goal")
    BUS.add_goal_task(
        db, goal_id="g", task_id="t", assignee=assignee, repo_id="automation",
        created_by="gemini", description="Do work",
    )
    run = BUS.claim_goal_task(db, assignee=assignee, worker_id="worker")
    assert run is not None
    BUS.attention_goal_task_run(
        db, run_id=run["run_id"], assignee=assignee, lease_token=run["lease_token"],
        error_code=code, detail=detail,
    )
    return int(run["run_id"])


def test_gemini_permission_failure_reassigns_directly_to_chatgpt(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"
    run_id = _attention(
        db, assignee="gemini", code="MODEL_DID_NOT_COMPLETE",
        detail='headless mode auto-denied the required "read_file" permission',
    )
    result = RECOVERY.recover_run(db, run_id=run_id)
    assert result["action"] == "goal_task_recovery_resumed"
    task = BUS.goal_status(db, goal_id="g")["tasks"][0]
    assert task["status"] == "open"
    assert task["assignee"] == "chatgpt"
    assert BUS.goal_task_run_status(db, goal_id="g")["runs"][0]["status"] == "acknowledged"


def test_uncertain_failure_creates_separate_recovery_task(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"
    run_id = _attention(
        db, assignee="chatgpt", code="MODEL_TIMEOUT",
        detail="provider timed out after execution may have started",
    )
    result = RECOVERY.recover_run(db, run_id=run_id)
    assert result["action"] == "goal_task_recovery_scheduled"
    assert result["task_id"] == f"recover-r{run_id}-a1"
    state = BUS.goal_status(db, goal_id="g")
    original = next(row for row in state["tasks"] if row["task_id"] == "t")
    recovery = next(row for row in state["tasks"] if row["task_id"] == result["task_id"])
    assert original["status"] == "in_progress"
    assert recovery["task_kind"] == "recovery"
    assert recovery["assignee"] == "chatgpt"
    assert state["summary"]["total"] == 1
    assert state["summary"]["recovery_total"] == 1


def test_completed_recovery_requeues_original_task(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"
    run_id = _attention(
        db, assignee="chatgpt", code="MODEL_TIMEOUT", detail="uncertain execution",
    )
    scheduled = RECOVERY.recover_run(db, run_id=run_id)
    recovery_task = scheduled["task_id"]
    run = BUS.claim_goal_task(db, assignee="chatgpt", worker_id="recovery-worker")
    assert run is not None and run["task_id"] == recovery_task
    BUS.complete_goal_task_run(
        db, run_id=run["run_id"], assignee="chatgpt", lease_token=run["lease_token"],
        result_status="completed", result="Inspected current state and repaired the cause.",
    )
    result = RECOVERY.sweep(db)
    resumed = next(row for row in result["actions"] if row["action"] == "goal_task_recovery_resumed")
    assert resumed["original_run_id"] == run_id
    original = next(row for row in BUS.goal_status(db, goal_id="g")["tasks"] if row["task_id"] == "t")
    assert original["status"] == "open"
    assert original["assignee"] == "chatgpt"


def test_recovery_uses_cross_provider_fallback_before_user_escalation(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"
    run_id = _attention(
        db, assignee="chatgpt", code="MODEL_TIMEOUT", detail="uncertain execution",
    )
    seen_assignees = []
    scheduled = RECOVERY.recover_run(db, run_id=run_id)
    for attempt in range(1, RECOVERY.MAX_RECOVERY_ATTEMPTS + 1):
        assert scheduled["attempt"] == attempt
        who = scheduled["assignee"]
        seen_assignees.append(who)
        recovery_run = BUS.claim_goal_task(db, assignee=who, worker_id=f"worker-{attempt}")
        assert recovery_run is not None
        BUS.attention_goal_task_run(
            db, run_id=recovery_run["run_id"], assignee=who,
            lease_token=recovery_run["lease_token"], error_code="MODEL_TIMEOUT",
            detail="recovery attempt also timed out",
        )
        outcome = RECOVERY.recover_run(db, run_id=int(recovery_run["run_id"]))
        scheduled = outcome
    assert seen_assignees == list(RECOVERY.RECOVERY_ASSIGNEES)
    assert outcome["action"] == "goal_task_recovery_escalated"
    final = RECOVERY.recover_run(db, run_id=run_id)
    assert final["action"] == "goal_task_recovery_not_needed"
    original = next(row for row in BUS.goal_status(db, goal_id="g")["tasks"] if row["task_id"] == "t")
    assert original["status"] == "user_decision_required"
