from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


MODULE = Path(__file__).resolve().parents[1] / "bin" / "project_control_http_watchdog.py"


def load_module():
    spec = importlib.util.spec_from_file_location("project_control_http_watchdog_test", MODULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_healthy_endpoint_does_not_restart(monkeypatch):
    mod = load_module()
    monkeypatch.setattr(mod, "healthy", lambda url, timeout: True)
    monkeypatch.setattr(sys, "argv", ["watchdog"])
    monkeypatch.setattr(mod.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not restart")))
    assert mod.main() == 0


def test_unhealthy_endpoint_restarts_exact_service(monkeypatch):
    mod = load_module()
    seen = {}
    monkeypatch.setattr(mod, "healthy", lambda url, timeout: False)
    monkeypatch.setattr(sys, "argv", ["watchdog"])

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    assert mod.main() == 0
    assert seen["argv"] == ["/usr/bin/systemctl", "restart", "project-control-http.service"]
