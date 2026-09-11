from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "deploy" / "systemd"


def _text(name: str) -> str:
    return (SYSTEMD / name).read_text(encoding="utf-8")


def test_project_control_service_has_survival_priority():
    text = _text("project-control-http.service")
    assert "Restart=always" in text
    assert "RestartSec=1" in text
    assert "OOMScoreAdjust=-700" in text
    assert "CPUWeight=10000" in text
    assert "IOWeight=10000" in text
    assert "MemoryLow=96M" in text
    assert text.index("StartLimitIntervalSec=0") < text.index("[Service]")


def test_watchdog_does_not_require_project_control_to_be_alive():
    text = _text("project-control-http-watchdog.service")
    assert "Requires=project-control-http.service" not in text
    assert "OOMScoreAdjust=-800" in text
    assert "CPUWeight=10000" in text
    assert "IOWeight=10000" in text


def test_independent_guardian_is_frequent_and_root_owned():
    service = _text("control-plane-guardian.service")
    timer = _text("control-plane-guardian.timer")
    assert "User=root" in service
    assert "control_plane_guardian.py" in service
    assert "OnUnitActiveSec=15s" in timer
    assert "Persistent=true" in timer


def test_oracle_browser_is_lower_priority_than_control_plane():
    text = _text("oracle-browser@.service")
    assert "Nice=15" in text
    assert "OOMScoreAdjust=500" in text
    assert "CPUWeight=25" in text
    assert "IOWeight=10" in text
    assert "MemoryHigh=420M" in text
    assert "MemoryMax=560M" in text


def test_chatgpt_browser_worker_is_also_disposable():
    text = _text("chatgpt-server-worker@.service")
    assert "Type=oneshot" in text
    assert "--serve" not in text
    assert "Restart=always" not in text
    assert "Nice=15" in text
    assert "OOMScoreAdjust=500" in text
    assert "CPUWeight=25" in text
    assert "IOWeight=10" in text
    assert "MemoryHigh=220M" in text
    assert "MemoryMax=320M" in text


def test_browser_demand_controller_is_lightweight_and_periodic():
    service = _text("chatgpt-browser-demand.service")
    timer = _text("chatgpt-browser-demand.timer")
    assert "User=root" in service
    assert "chatgpt_browser_demand.py" in service
    assert "OnUnitActiveSec=20s" in timer
    assert "Persistent=true" in timer


def test_goal_progress_has_its_own_timer():
    service = _text("goal-progress-notify.service")
    timer = _text("goal-progress-notify.timer")
    assert "User=board" in service
    assert "goal_progress_notify.py" in service
    assert "OnUnitActiveSec=30s" in timer
    assert "Persistent=true" in timer
