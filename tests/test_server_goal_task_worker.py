from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


BIN = Path(__file__).resolve().parents[1] / "bin"

def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, BIN / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

BUS = _load("chatgpt_server_bus", "chatgpt_server_bus.py")
WORKER = _load("server_goal_task_worker", "server_goal_task_worker.py")


def _task(db: Path, assignee: str = "codex") -> None:
    BUS.create_goal(db, goal_id="g", owner="gemini", created_by="user", description="Improve the repo.")
    BUS.add_goal_task(db, goal_id="g", task_id="t", assignee=assignee,
                      created_by="gemini", description="Make and verify one safe change.")


def test_codex_goal_task_uses_workspace_write_and_completes(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"
    repo = tmp_path / "repo"; repo.mkdir()
    _task(db)
    seen = {}

    def execute(argv, **kwargs):
        seen["argv"] = list(argv); seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0,
            stdout='{"status":"completed","result":"changed file and tests passed"}', stderr="")

    result = WORKER.run_one(db_path=db, repo=repo, assignees=("codex",),
                            codex=tmp_path / "codex", execute=execute)
    assert result["result_status"] == "completed"
    assert "workspace-write" in seen["argv"]
    assert seen["kwargs"]["cwd"] == repo
    assert "irreversible external action" in seen["kwargs"]["input"]
    assert BUS.goal_status(db, goal_id="g")["tasks"][0]["status"] == "completed"


def test_timeout_freezes_run_without_requeue(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"; repo = tmp_path / "repo"; repo.mkdir(); _task(db)

    def execute(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 10)

    result = WORKER.run_one(db_path=db, repo=repo, assignees=("codex",),
                            codex=tmp_path / "codex", execute=execute)
    assert result["action"] == "goal_task_run_attention"
    assert result["automatic_retry"] is False
    assert BUS.goal_status(db, goal_id="g")["tasks"][0]["status"] == "in_progress"
    assert WORKER.run_one(db_path=db, repo=repo, assignees=("codex",),
                          codex=tmp_path / "codex", execute=execute)["reason"] == "no_executable_goal_tasks"


def test_user_decision_result_stops_task(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"; repo = tmp_path / "repo"; repo.mkdir(); _task(db, "gemini")

    def execute(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=(
            '{"status":"user_decision_required","result":"prepared the safe part",'
            '"blocker":"Publishing requires explicit user approval"}'
        ), stderr="")

    result = WORKER.run_one(db_path=db, repo=repo, assignees=("gemini",),
                            agy=tmp_path / "agy", execute=execute)
    assert result["result_status"] == "user_decision_required"
    assert BUS.goal_status(db, goal_id="g")["tasks"][0]["status"] == "user_decision_required"


def test_busy_lock_does_not_claim(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"; repo = tmp_path / "repo"; repo.mkdir(); _task(db)
    lock = tmp_path / "provider.lock"
    with BUS.provider_slot(lock) as acquired:
        assert acquired
        result = WORKER.run_one(db_path=db, repo=repo, assignees=("codex",),
                                codex=tmp_path / "codex", provider_lock=lock)
    assert result == {"action": "wait", "reason": "provider_busy"}
    assert BUS.goal_status(db, goal_id="g")["tasks"][0]["status"] == "open"
