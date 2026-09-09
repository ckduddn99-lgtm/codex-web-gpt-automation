from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "deploy" / "systemd" / "user" / "board-goal-driver.service"
TIMER = ROOT / "deploy" / "systemd" / "user" / "board-goal-driver.timer"


def test_user_goal_service_keeps_provider_and_discord_boundaries() -> None:
    text = SERVICE.read_text(encoding="utf-8")
    assert "User=" not in text and "Group=" not in text
    assert "sudo" not in text.casefold()
    assert "WorkingDirectory=%h/codex-web-gpt-automation" in text
    assert "PATH=/snap/bin:" in text
    assert "PYTHONUTF8=1" in text
    assert "server_goal_driver.py" in text and "advance --all" in text
    assert "--provider-lock %h/.local/state/ai-bus/provider.lock" in text
    assert "board_notify.py --channel 일반" in text
    assert "--channel script" not in text
    assert "SuccessExitStatus=0 2" in text
    assert "TimeoutStartSec=8min" in text


def test_user_goal_timer_never_overlaps_the_previous_oneshot() -> None:
    text = TIMER.read_text(encoding="utf-8")
    assert "OnStartupSec=2min" in text
    assert "OnUnitInactiveSec=2min" in text
    assert "OnUnitActiveSec" not in text
    assert "Unit=board-goal-driver.service" in text
    assert "WantedBy=timers.target" in text


def test_goal_driver_docs_keep_install_and_execution_boundaries_explicit() -> None:
    docs = (ROOT / "docs" / "SERVER_AI_BUS.md").read_text(encoding="utf-8")
    assert "systemctl --user" in docs
    assert "goal_tasks" in docs
    assert "does not execute" in docs
    assert "enable-linger" in docs


def test_goal_timer_and_its_runtime_dependencies_ship_in_release_surfaces() -> None:
    manifest = set(json.loads((ROOT / "install-manifest.json").read_text(encoding="utf-8"))["include"])
    package = set(json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["files"])
    required = {
        "bin/agy_server_worker.py",
        "bin/board_seat.py",
        "bin/board_notify.py",
        "bin/chatgpt_server_bus.py",
        "bin/server_goal_driver.py",
        "deploy/systemd/user/board-goal-driver.service",
        "deploy/systemd/user/board-goal-driver.timer",
    }
    assert required <= manifest
    assert required <= package
