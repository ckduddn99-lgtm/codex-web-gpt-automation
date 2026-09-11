from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


MODULE = Path(__file__).resolve().parents[1] / "bin" / "control_plane_guardian.py"


def load_module():
    spec = importlib.util.spec_from_file_location("control_plane_guardian_test", MODULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_healthy_services_do_nothing(monkeypatch, tmp_path: Path):
    mod = load_module()
    monkeypatch.setattr(mod, "_active", lambda service: True)
    monkeypatch.setattr(mod, "_healthy", lambda: True)
    monkeypatch.setattr(mod, "pressure_snapshot", lambda: {
        "cpu_count": 2, "load1": 0.2, "memory_available": 512 * 1024 * 1024,
        "swap_free": 1024 * 1024 * 1024, "swap_total": 2 * 1024 * 1024 * 1024,
        "memory_psi_full_avg10": 0.0, "io_psi_full_avg10": 0.0,
    })
    monkeypatch.setattr(mod, "_restart", lambda service: (_ for _ in ()).throw(AssertionError("must not restart")))
    result = mod.run_once(state_path=tmp_path / "state.json", now=1000.0)
    assert result["ok"] is True
    assert result["actions"] == []
    assert result["critical_pressure"] is False


def test_unhealthy_project_control_requires_consecutive_misses(monkeypatch, tmp_path: Path):
    mod = load_module()
    restarted = []
    monkeypatch.setattr(mod, "_active", lambda service: True)
    monkeypatch.setattr(mod, "_healthy", lambda: False)
    monkeypatch.setattr(mod, "_restart", lambda service: restarted.append(service) or True)
    monkeypatch.setattr(mod, "pressure_snapshot", lambda: {
        "cpu_count": 2, "load1": 0.2, "memory_available": 512 * 1024 * 1024,
        "swap_free": 1024 * 1024 * 1024, "swap_total": 2 * 1024 * 1024 * 1024,
        "memory_psi_full_avg10": 0.0, "io_psi_full_avg10": 0.0,
    })
    state = tmp_path / "state.json"
    assert mod.run_once(state_path=state, health_failure_threshold=3, now=1000.0)["actions"] == []
    assert mod.run_once(state_path=state, health_failure_threshold=3, now=1010.0)["actions"] == []
    third = mod.run_once(state_path=state, health_failure_threshold=3, now=1020.0)
    assert restarted == [mod.PROJECT_CONTROL_SERVICE]
    assert third["actions"][0]["misses"] == 3


def test_dead_commander_and_tailscale_restart_immediately(monkeypatch, tmp_path: Path):
    mod = load_module()
    restarted = []
    monkeypatch.setattr(mod, "_active", lambda service: service == mod.PROJECT_CONTROL_SERVICE)
    monkeypatch.setattr(mod, "_healthy", lambda: True)
    monkeypatch.setattr(mod, "_restart", lambda service: restarted.append(service) or True)
    monkeypatch.setattr(mod, "pressure_snapshot", lambda: {
        "cpu_count": 2, "load1": 0.2, "memory_available": 512 * 1024 * 1024,
        "swap_free": 1024 * 1024 * 1024, "swap_total": 2 * 1024 * 1024 * 1024,
        "memory_psi_full_avg10": 0.0, "io_psi_full_avg10": 0.0,
    })
    result = mod.run_once(state_path=tmp_path / "state.json", now=1000.0)
    assert restarted == [mod.TAILSCALE_SERVICE, mod.COMMANDER_SERVICE]
    assert len(result["actions"]) == 2


def test_emergency_pressure_sheds_worker_and_browser(monkeypatch, tmp_path: Path):
    mod = load_module()
    stopped = []
    monkeypatch.setattr(mod, "_active", lambda service: True)
    monkeypatch.setattr(mod, "_healthy", lambda: True)
    monkeypatch.setattr(mod, "_stop", lambda service: stopped.append(service) or True)
    monkeypatch.setattr(mod, "_cleanup", lambda: {"attempted": False, "reason": "test"})
    monkeypatch.setattr(mod, "pressure_snapshot", lambda: {
        "cpu_count": 2, "load1": 12.0, "memory_available": 96 * 1024 * 1024,
        "swap_free": 1024 * 1024 * 1024, "swap_total": 2 * 1024 * 1024 * 1024,
        "memory_psi_full_avg10": 50.0, "io_psi_full_avg10": 60.0,
    })
    result = mod.run_once(state_path=tmp_path / "state.json", now=1000.0)
    assert stopped == [mod.BROWSER_WORKER_SERVICE, mod.BROWSER_SERVICE]
    assert result["emergency_pressure"] is True
    assert [item["action"] for item in result["actions"][:2]] == ["emergency-stop", "emergency-stop"]


def test_pressure_cleanup_is_cooldown_bounded(monkeypatch, tmp_path: Path):
    mod = load_module()
    calls = []
    monkeypatch.setattr(mod, "_active", lambda service: True)
    monkeypatch.setattr(mod, "_healthy", lambda: True)
    monkeypatch.setattr(mod, "pressure_snapshot", lambda: {
        "cpu_count": 2, "load1": 12.0, "memory_available": 128 * 1024 * 1024,
        "swap_free": 128 * 1024 * 1024, "swap_total": 2 * 1024 * 1024 * 1024,
        "memory_psi_full_avg10": 30.0, "io_psi_full_avg10": 30.0,
    })
    monkeypatch.setattr(mod, "_cleanup", lambda: calls.append(1) or {"attempted": True, "ok": True})
    state = tmp_path / "state.json"
    first = mod.run_once(state_path=state, cleanup_cooldown=900.0, now=1000.0)
    second = mod.run_once(state_path=state, cleanup_cooldown=900.0, now=1200.0)
    assert first["cleanup"]["ok"] is True
    assert second["cleanup"] is None
    assert calls == [1]
