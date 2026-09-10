from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "bin" / "project_control_ops.py"


def load_module():
    spec = importlib.util.spec_from_file_location("project_control_ops_test", MODULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_helper_rejects_non_root(monkeypatch):
    mod = load_module()
    monkeypatch.setattr(mod.os, "geteuid", lambda: 1000)
    assert mod.main(["project-control-ops", "start", "oracle-browser@board.service"]) == 77


def test_helper_rejects_unlisted_service(monkeypatch):
    mod = load_module()
    monkeypatch.setattr(mod.os, "geteuid", lambda: 0)
    called = False

    def fake_run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("systemctl must not run")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    assert mod.main(["project-control-ops", "restart", "ssh.service"]) == 65
    assert called is False


def test_helper_executes_exact_systemctl_for_allowlisted_service(monkeypatch):
    mod = load_module()
    monkeypatch.setattr(mod.os, "geteuid", lambda: 0)
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    rc = mod.main(["project-control-ops", "start", "oracle-browser@board.service"])
    assert rc == 0
    assert seen["argv"] == ["/usr/bin/systemctl", "start", "oracle-browser@board.service"]
    assert seen["kwargs"]["timeout"] == 120
