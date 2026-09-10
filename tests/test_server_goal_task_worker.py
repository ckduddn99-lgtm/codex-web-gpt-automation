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


def _task(db: Path, assignee: str = "codex", *, repo_id: str = "automation",
          goal: str = "Improve the repo.", task: str = "Make and verify one safe change.") -> None:
    BUS.create_goal(db, goal_id="g", owner="gemini", created_by="user", description=goal)
    BUS.add_goal_task(db, goal_id="g", task_id="t", assignee=assignee, repo_id=repo_id,
                      created_by="gemini", description=task)


def test_codex_goal_task_uses_workspace_write_and_completes(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"
    repo = tmp_path / "repo"; repo.mkdir()
    _task(db)
    seen = {}

    def execute(argv, **kwargs):
        seen["argv"] = list(argv); seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0,
            stdout='{"status":"completed","result":"changed file and tests passed"}', stderr="")

    result = WORKER.run_one(db_path=db, repo=repo, repo_routes={"automation": repo},
                            assignees=("codex",), codex=tmp_path / "codex", execute=execute)
    assert result["result_status"] == "completed"
    assert "workspace-write" in seen["argv"]
    assert seen["kwargs"]["cwd"] == repo
    assert seen["kwargs"]["env"]["GIT_CONFIG_KEY_0"] == "safe.directory"
    assert seen["kwargs"]["env"]["GIT_CONFIG_VALUE_0"] == str(repo.resolve())
    assert "irreversible external action" in seen["kwargs"]["input"]
    assert BUS.goal_status(db, goal_id="g")["tasks"][0]["status"] == "completed"


def test_codex_goal_task_routes_only_to_named_allowed_repo(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"
    default_repo = tmp_path / "default"; default_repo.mkdir()
    stock_repo = tmp_path / "stock"; stock_repo.mkdir()
    _task(db, repo_id="stock", goal="Improve automation even though this text says stock-ai-app.")
    seen = {}

    def execute(argv, **kwargs):
        seen["cwd"] = kwargs["cwd"]
        return subprocess.CompletedProcess(argv, 0,
            stdout='{"status":"completed","result":"verified routed repository"}', stderr="")

    result = WORKER.run_one(
        db_path=db, repo=default_repo,
        repo_routes={"automation": default_repo, "stock": stock_repo},
        assignees=("codex",), codex=tmp_path / "codex", execute=execute,
    )
    assert result["result_status"] == "completed"
    assert seen["cwd"] == stock_repo


def test_legacy_unassigned_repo_fails_closed_before_provider_call(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"
    repo = tmp_path / "repo"; repo.mkdir()
    _task(db, goal="stock-ai-app is mentioned here but must not be inferred")
    with BUS.connect(db) as state:
        state.execute(
            "UPDATE goal_tasks SET repo_id = ? WHERE goal_id = 'g' AND task_id = 't'",
            (BUS.LEGACY_REPO_ID,),
        )
    called = False

    def execute(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("provider must not run without explicit repo binding")

    result = WORKER.run_one(
        db_path=db, repo=repo, repo_routes={"automation": repo},
        assignees=("codex",), codex=tmp_path / "codex", execute=execute,
    )
    assert result["action"] == "goal_task_run_attention"
    assert result["error_code"] == "GOAL_REPO_ROUTE_INVALID"
    assert called is False


def test_chatgpt_goal_task_attaches_to_managed_browser(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"; repo = tmp_path / "repo"; repo.mkdir(); _task(db, "chatgpt")
    profile = tmp_path / "profile"; profile.mkdir()
    npx = tmp_path / "npx"; npx.write_text("fixture", encoding="utf-8")
    seen = {}

    def execute(argv, **kwargs):
        seen["argv"] = list(argv)
        output = Path(argv[argv.index("--write-output") + 1])
        output.write_text('{"status":"completed","result":"verified"}', encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="done", stderr="")

    result = WORKER.run_one(
        db_path=db, repo=repo, repo_routes={"automation": repo}, assignees=("chatgpt",),
        profile=profile, state_dir=tmp_path / "runs", npx=npx, execute=execute,
    )
    assert result["result_status"] == "completed"
    assert "--copy-profile" not in seen["argv"]
    assert "--browser-attach-running" in seen["argv"]
    assert seen["argv"][seen["argv"].index("--remote-chrome") + 1] == "127.0.0.1:9222"


def test_timeout_freezes_run_without_requeue(tmp_path: Path) -> None:
    db = tmp_path / "bus.sqlite3"; repo = tmp_path / "repo"; repo.mkdir(); _task(db)

    def execute(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 10)

    result = WORKER.run_one(db_path=db, repo=repo, repo_routes={"automation": repo},
                            assignees=("codex",), codex=tmp_path / "codex", execute=execute)
    assert result["action"] == "goal_task_run_attention"
    assert result["automatic_retry"] is False
    assert "timed out" in result["public_reason"]
    assert BUS.goal_status(db, goal_id="g")["tasks"][0]["status"] == "in_progress"
    assert WORKER.run_one(db_path=db, repo=repo, repo_routes={"automation": repo},
                          assignees=("codex",), codex=tmp_path / "codex", execute=execute)["reason"] == "no_executable_goal_tasks"


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
        result = WORKER.run_one(db_path=db, repo=repo, repo_routes={"automation": repo},
                                assignees=("codex",), codex=tmp_path / "codex", provider_lock=lock)
    assert result == {"action": "wait", "reason": "provider_busy"}
    assert BUS.goal_status(db, goal_id="g")["tasks"][0]["status"] == "open"
