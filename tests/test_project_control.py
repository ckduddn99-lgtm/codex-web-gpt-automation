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
    recovery_calls: list[Path] = []

    def sweep(db_path):
        recovery_calls.append(Path(db_path))
        return {"action": "goal_task_recovery_sweep", "actions": []}

    def run_one(**kwargs):
        seen["worker"] = kwargs
        return {"action": "wait", "reason": "none"}

    def advance_all(**kwargs):
        seen["manager"] = kwargs
        return {"action": "wait", "reason": "none"}

    monkeypatch.setattr(control.RECOVERY, "sweep", sweep)
    monkeypatch.setattr(control.WORKER, "run_one", run_one)
    monkeypatch.setattr(control.GOAL, "advance_all", advance_all)
    result = control.tick(db, registry={"automation": auto, "stock": stock})
    assert result["action"] == "project_tick"
    assert seen["worker"]["repo_routes"] == {"automation": auto, "stock": stock}
    assert seen["manager"]["repo_ids"] == ("automation", "stock")
    assert recovery_calls == [db, db]


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
        "project_repo_read", "project_repo_search", "project_repo_patch",
        "project_repo_test", "project_repo_git_status", "project_repo_diff",
        "project_repo_commit",
    }


def test_mcp_server_waits_for_async_tool_call_before_eof_exit() -> None:
    server = Path(__file__).resolve().parents[1] / "mcp_servers" / "project-control" / "server.mjs"
    proc = subprocess.run(
        ["node", str(server)],
        input='{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"project_repo_git_status","arguments":{"repo_id":"automation"}}}\n',
        text=True, capture_output=True, timeout=10, check=False,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.strip())
    assert payload["id"] == 2
    content = json.loads(payload["result"]["content"][0]["text"])
    assert content["action"] == "project_repo_git_status"
    assert content["repo_id"] == "automation"


def test_repo_read_and_patch_are_hash_bound(tmp_path: Path) -> None:
    control = load_control()
    repo = tmp_path / "repo"; repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    target = repo / "sample.txt"; target.write_text("hello\nworld\n", encoding="utf-8")
    registry = {"automation": repo}
    read = control.repo_read(registry, repo_id="automation", path="sample.txt")
    assert read["text"] == "hello\nworld\n"
    patched = control.repo_patch(
        registry, repo_id="automation", path="sample.txt",
        expected_sha256=read["sha256"],
        replacements=[{"old_text": "world", "new_text": "there"}],
    )
    assert target.read_text(encoding="utf-8") == "hello\nthere\n"
    with pytest.raises(control.ProjectControlError, match="stale file hash"):
        control.repo_patch(
            registry, repo_id="automation", path="sample.txt",
            expected_sha256=read["sha256"],
            replacements=[{"old_text": "there", "new_text": "again"}],
        )
    assert patched["new_sha256"] != read["sha256"]


def test_repo_paths_reject_escape_and_symlink(tmp_path: Path) -> None:
    control = load_control()
    repo = tmp_path / "repo"; repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    outside = tmp_path / "outside.txt"; outside.write_text("secret", encoding="utf-8")
    (repo / "link.txt").symlink_to(outside)
    real_dir = repo / "real"; real_dir.mkdir()
    (real_dir / "nested.txt").write_text("nested", encoding="utf-8")
    (repo / "dirlink").symlink_to(real_dir, target_is_directory=True)
    registry = {"automation": repo}
    with pytest.raises(control.ProjectControlError):
        control.repo_read(registry, repo_id="automation", path="../outside.txt")
    with pytest.raises(control.ProjectControlError):
        control.repo_read(registry, repo_id="automation", path="link.txt")
    with pytest.raises(control.ProjectControlError, match="symlinks"):
        control.repo_read(registry, repo_id="automation", path="dirlink/nested.txt")


def test_repo_search_is_literal_and_bounded(tmp_path: Path) -> None:
    control = load_control()
    repo = tmp_path / "repo"; repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "a.txt").write_text("Alpha [literal]\n", encoding="utf-8")
    result = control.repo_search(
        {"automation": repo}, repo_id="automation", query="[literal]",
        case_sensitive=True, max_results=10,
    )
    assert result["matches"][0]["path"] == "a.txt"


def test_repo_test_rejects_non_test_pytest_target(tmp_path: Path) -> None:
    control = load_control()
    repo = tmp_path / "repo"; repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "script.py").write_text("print('x')\n", encoding="utf-8")
    with pytest.raises(control.ProjectControlError, match="tests/"):
        control.repo_test(
            {"automation": repo}, repo_id="automation", profile="pytest",
            targets=["script.py"], timeout=10,
        )


def test_repo_commit_only_commits_explicit_paths(tmp_path: Path) -> None:
    control = load_control()
    repo = tmp_path / "repo"; repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    result = control.repo_commit(
        {"automation": repo}, repo_id="automation", message="test: commit a",
        paths=["a.txt"],
    )
    assert result["commit"]
    status = subprocess.run(
        ["git", "status", "--short"], cwd=repo, check=True, text=True, capture_output=True,
    ).stdout
    assert "?? b.txt" in status
    assert "a.txt" not in status
