from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "bin" / "github_breakglass_command.py"


def load_module():
    spec = importlib.util.spec_from_file_location("github_breakglass_command_test", MODULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_unknown_command_fails_closed(monkeypatch):
    mod = load_module()
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "uname -a")
    assert mod.main() == 64


def test_restart_pc_maps_only_to_allowlisted_helper(monkeypatch):
    mod = load_module()
    calls = []
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "/restart_pc")
    monkeypatch.setattr(mod, "service", lambda action, unit: calls.append((action, unit)) or 0)
    monkeypatch.setattr(mod, "healthy", lambda: True)
    assert mod.main() == 0
    assert calls == [("restart", mod.PROJECT_CONTROL)]


def test_stop_browser_stops_worker_before_browser(monkeypatch):
    mod = load_module()
    calls = []
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "/stop_browser")
    monkeypatch.setattr(mod, "service", lambda action, unit: calls.append((action, unit)) or 0)
    assert mod.main() == 0
    assert calls == [("stop", mod.WORKER), ("stop", mod.BROWSER)]
