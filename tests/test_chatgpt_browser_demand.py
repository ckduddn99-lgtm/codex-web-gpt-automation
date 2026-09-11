from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "bin" / "chatgpt_browser_demand.py"


def load_module():
    spec = importlib.util.spec_from_file_location("chatgpt_browser_demand_test", MODULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_no_pending_stops_only_managed_idle_browser(monkeypatch, tmp_path: Path):
    mod = load_module()
    mod.STATE_PATH = tmp_path / "state.json"
    mod._save_state({"managed_browser": True})
    monkeypatch.setattr(mod, "_pending", lambda db: 0)
    monkeypatch.setattr(mod, "_active", lambda service: service == mod.BROWSER)
    monkeypatch.setattr(mod, "pressure_snapshot", lambda: {})
    calls = []
    monkeypatch.setattr(mod, "_systemctl", lambda action, service, timeout=45.0: calls.append((action, service)) or True)
    result = mod.run_once(tmp_path / "bus.sqlite3")
    assert result["ok"] is True
    assert calls == [("stop", mod.BROWSER)]


def test_pending_defers_start_under_pressure(monkeypatch, tmp_path: Path):
    mod = load_module()
    mod.STATE_PATH = tmp_path / "state.json"
    monkeypatch.setattr(mod, "_pending", lambda db: 1)
    monkeypatch.setattr(mod, "_active", lambda service: False)
    monkeypatch.setattr(mod, "pressure_snapshot", lambda: {
        "memory_available": 128 * 1024 * 1024,
        "io_psi_full_avg10": 50.0,
        "memory_psi_full_avg10": 20.0,
    })
    monkeypatch.setattr(mod, "_systemctl", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not start")))
    result = mod.run_once(tmp_path / "bus.sqlite3")
    assert result["ok"] is True
    assert result["actions"][0]["action"] == "start-deferred-pressure"


def test_pending_starts_browser_then_one_worker(monkeypatch, tmp_path: Path):
    mod = load_module()
    mod.STATE_PATH = tmp_path / "state.json"
    monkeypatch.setattr(mod, "_pending", lambda db: 2)
    monkeypatch.setattr(mod, "_active", lambda service: False)
    monkeypatch.setattr(mod, "pressure_snapshot", lambda: {
        "memory_available": 600 * 1024 * 1024,
        "io_psi_full_avg10": 0.0,
        "memory_psi_full_avg10": 0.0,
    })
    calls = []
    monkeypatch.setattr(mod, "_systemctl", lambda action, service, timeout=45.0: calls.append((action, service)) or True)
    monkeypatch.setattr(mod, "_cdp_ready", lambda timeout=45.0: True)
    result = mod.run_once(tmp_path / "bus.sqlite3")
    assert result["ok"] is True
    assert calls == [("start", mod.BROWSER), ("start", mod.WORKER)]
