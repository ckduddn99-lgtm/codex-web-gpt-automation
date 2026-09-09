from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"
MODULE = BIN / "project_control.py"


def load_control():
    name = "project_control_test"
    spec = importlib.util.spec_from_file_location(name, MODULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_registry_defaults_to_automation_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    control = load_control()
    monkeypatch.delenv("PROJECT_CONTROL_REPOS_JSON", raising=False)
    registry = control.load_repo_registry([])
    assert registry["automation"] == control.REPO_ROOT


def test_add_task_requires_registered_repo_id(tmp_path: Path) -> None:
    control = load_control()
    db = tmp_path / "bus.sqlite3"
    control.create_goal(db, goal_id="g", description="Goal")
    with pytest.raises(control.ProjectControlError):
        control.add_task(
            db, registry={"automation": tmp_path}, goal_id="g", task_id="t",
            assignee="codex", repo_id="stock", description="Task",
        )


def test_add_task_persists_repo_id(tmp_path: Path) -> None:
    control = load_control()
    db = tmp_path / "bus.sqlite3"
    repo = tmp_path / "repo"; repo.mkdir()
    control.create_goal(db, goal_id="g", description="Goal")
    result = control.add_task(
        db, registry={"automation": repo}, goal_id="g", task_id="t",
        assignee="codex", repo_id="automation", description="Task",
    )
    assert result["repo_id"] == "automation"
    task = control.BUS.goal_status(db, goal_id="g")["tasks"][0]
    assert task["repo_id"] == "automation"


def test_tick_passes_same_registry_to_worker_and_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    control = load_control()
    db = tmp_path / "bus.sqlite3"
    auto = tmp_path / "automation"; auto.mkdir()
    stock = tmp_path / "stock"; stock.mkdir()
    seen = {}

    def run_one(**kwargs):
        seen["worker"] = kwargs
        return {"action": "wait", "reason": "none"}

    def advance_all(**kwargs):
        seen["manager"] = kwargs
        return {"action": "wait", "reason": "none"}

    monkeypatch.setattr(control.WORKER, "run_one", run_one)
    monkeypatch.setattr(control.GOAL, "advance_all", advance_all)
    result = control.tick(db, registry={"automation": auto, "stock": stock})
    assert result["action"] == "project_tick"
    assert seen["worker"]["repo_routes"] == {"automation": auto, "stock": stock}
    assert seen["manager"]["repo_ids"] == ("automation", "stock")


def test_mcp_server_lists_only_project_control_tools(tmp_path: Path) -> None:
    server = Path(__file__).resolve().parents[1] / "mcp_servers" / "project-control" / "server.mjs"
    proc = subprocess.run(
        ["node", str(server)],
        input='{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}\n',
        text=True, capture_output=True, timeout=10, check=False,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.strip())
    names = {tool["name"] for tool in payload["result"]["tools"]}
    assert names == {
        "project_repos", "project_backlog", "project_goal_status",
        "project_goal_create", "project_task_add", "project_tick",
    }
