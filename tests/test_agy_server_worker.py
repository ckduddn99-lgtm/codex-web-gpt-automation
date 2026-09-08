from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path


BIN = Path(__file__).resolve().parents[1] / "bin"


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, BIN / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


BUS = load("chatgpt_server_bus", "chatgpt_server_bus.py")
WORKER = load("agy_server_worker", "agy_server_worker.py")


def _round(tmp_path: Path) -> Path:
    db = tmp_path / "ai-bus.sqlite3"
    BUS.create_round(
        db, round_id="round-agy", sender="dispatcher",
        participants=["gemini", "fixture"], question="/goal inspect this safely",
    )
    return db


def test_worker_keeps_task_data_on_stdin_and_uses_plan_mode(tmp_path: Path) -> None:
    db = _round(tmp_path)
    agy = tmp_path / "agy"
    agy.write_text("fixture", encoding="utf-8")
    seen = {}

    def execute(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="independent answer\n", stderr="")

    result = WORKER.run_one(
        db_path=db, recipient="gemini", worker_id="gemini-antigravity",
        agy=agy, execute=execute,
    )
    assert result["status"] == "done"
    assert seen["argv"] == [
        str(agy), "--mode", "plan", "--output-format", "text",
        "--print-timeout", "5m",
    ]
    assert "/goal inspect this safely" not in " ".join(seen["argv"])
    packet = seen["kwargs"]["input"]
    assert "dispatcher>gemini deliberate" in packet
    assert "QUESTION_BEGIN ref=1" in packet
    assert "/goal inspect this safely" in packet
    assert "Do not call tools." in packet
    assert seen["kwargs"]["env"]["PATH"].startswith(str(agy.parent) + os.pathsep)


def test_worker_failure_requires_attention_and_never_requeues(tmp_path: Path) -> None:
    db = _round(tmp_path)
    agy = tmp_path / "agy"
    agy.write_text("fixture", encoding="utf-8")

    def execute(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="provider unavailable")

    result = WORKER.run_one(
        db_path=db, recipient="gemini", worker_id="gemini-antigravity",
        agy=agy, execute=execute,
    )
    assert result["status"] == "attention_required"
    assert BUS.claim(db, recipient="gemini", worker_id="other") is None
