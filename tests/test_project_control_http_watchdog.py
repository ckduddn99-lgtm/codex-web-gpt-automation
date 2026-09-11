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


def test_healthy_endpoint_does_not_restart(monkeypatch, tmp_path: Path):
    mod = load_module()
    monkeypatch.setattr(mod, "healthy", lambda url, timeout: True)
    monkeypatch.setattr(mod, "_notify_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "argv", ["watchdog", "--state-path", str(tmp_path / "failures")])
    monkeypatch.setattr(mod.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not restart")))
    assert mod.main() == 0


def test_unhealthy_endpoint_restarts_exact_service(monkeypatch, tmp_path: Path):
    mod = load_module()
    seen = {}
    monkeypatch.setattr(mod, "healthy", lambda url, timeout: False)
    monkeypatch.setattr(mod, "_notify_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "argv", [
        "watchdog", "--state-path", str(tmp_path / "failures"), "--failure-threshold", "1",
    ])

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    assert mod.main() == 0
    assert seen["argv"] == ["/usr/bin/systemctl", "restart", "project-control-http.service"]


def test_transient_failures_do_not_restart_until_threshold(monkeypatch, tmp_path: Path):
    mod = load_module()
    calls: list[list[str]] = []
    monkeypatch.setattr(mod, "healthy", lambda url, timeout: False)
    monkeypatch.setattr(mod, "_notify_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "argv", ["watchdog", "--state-path", str(tmp_path / "failures")])

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    assert mod.main() == 0 and calls == []
    assert mod.main() == 0 and calls == []
    assert mod.main() == 0 and len(calls) == 1


def test_healthy_watchdog_also_runs_progress_heartbeat(monkeypatch, tmp_path: Path):
    mod = load_module()
    seen: list[tuple[Path, Path, str]] = []
    monkeypatch.setattr(mod, "healthy", lambda url, timeout: True)
    monkeypatch.setattr(mod, "_notify_progress", lambda script, db, user: seen.append((script, db, user)))
    monkeypatch.setattr(sys, "argv", [
        "watchdog", "--state-path", str(tmp_path / "failures"),
        "--progress-script", str(tmp_path / "notify.py"),
        "--progress-db", str(tmp_path / "bus.sqlite3"),
    ])
    assert mod.main() == 0
    assert seen == [(tmp_path / "notify.py", tmp_path / "bus.sqlite3", "board")]
