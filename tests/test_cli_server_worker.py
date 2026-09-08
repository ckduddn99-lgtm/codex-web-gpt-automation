from __future__ import annotations

import importlib.util
import os
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
WORKER = load("cli_server_worker", "cli_server_worker.py")


def _round(tmp_path: Path, participant: str) -> Path:
    db = tmp_path / "ai-bus.sqlite3"
    BUS.create_round(
        db, round_id=f"round-{participant}", sender="gemini",
        participants=[participant, "fixture"], question="Review this without running it.",
    )
    return db


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        (
            "codex",
            ["exec", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral",
             "--ignore-user-config", "--ignore-rules", "--color", "never", "-"],
        ),
        (
            "claude",
            ["-p", "--permission-mode", "plan", "--permission-prompts", "none",
             "--tools", "", "--safe-mode", "--strict-mcp-config",
             "--no-session-persistence", "--output-format", "text"],
        ),
    ],
)
def test_cli_worker_uses_stdin_and_read_only_mode(
    tmp_path: Path, provider: str, expected: list[str]
) -> None:
    db = _round(tmp_path, provider)
    cli = tmp_path / provider
    cli.write_text("fixture", encoding="utf-8")
    seen = {}

    def execute(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["kwargs"] = kwargs
        assert Path(kwargs["cwd"]).is_dir()
        return subprocess.CompletedProcess(argv, 0, stdout=f"{provider} answer\n", stderr="")

    result = WORKER.run_one(
        db_path=db, recipient=provider, worker_id=f"{provider}-worker",
        provider=provider, cli=cli, execute=execute,
    )
    assert result["status"] == "done"
    assert seen["argv"] == [str(cli), *expected]
    assert "Review this without running it." not in " ".join(seen["argv"])
    assert "Review this without running it." in seen["kwargs"]["input"]
    assert "Do not modify files" in seen["kwargs"]["input"]
    assert seen["kwargs"]["env"]["PATH"].startswith(str(cli.parent) + os.pathsep)


def test_cli_failure_requires_attention_without_retry(tmp_path: Path) -> None:
    db = _round(tmp_path, "codex")
    cli = tmp_path / "codex"
    cli.write_text("fixture", encoding="utf-8")

    def execute(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="provider unavailable")

    result = WORKER.run_one(
        db_path=db, recipient="codex", worker_id="codex-worker",
        provider="codex", cli=cli, execute=execute,
    )
    assert result["status"] == "attention_required"
    assert BUS.claim(db, recipient="codex", worker_id="other") is None


def test_busy_provider_slot_does_not_claim_a_task(tmp_path: Path) -> None:
    db = _round(tmp_path, "claude")
    cli = tmp_path / "claude"
    cli.write_text("fixture", encoding="utf-8")
    lock = tmp_path / "provider.lock"

    with BUS.provider_slot(lock) as acquired:
        assert acquired
        result = WORKER.run_one(
            db_path=db, recipient="claude", worker_id="claude-worker",
            provider="claude", cli=cli, provider_lock=lock,
        )
        assert result == {"status": "busy"}

    task = BUS.claim(db, recipient="claude", worker_id="after-lock")
    assert task is not None
