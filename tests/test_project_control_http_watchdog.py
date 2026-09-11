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
    monkeypatch.setattr(mod, "_run_guardian", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod, "_notify_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "argv", ["watchdog", "--state-path", str(tmp_path / "failures")])
    monkeypatch.setattr(mod.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not restart")))
    assert mod.main() == 0


def test_unhealthy_endpoint_never_restarts_directly(monkeypatch, tmp_path: Path):
    mod = load_module()
    guardian_seen = []
    monkeypatch.setattr(mod, "healthy", lambda url, timeout: False)
    monkeypatch.setattr(mod, "_run_guardian", lambda script: guardian_seen.append(script))
    monkeypatch.setattr(mod, "_notify_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "argv", [
        "watchdog", "--state-path", str(tmp_path / "failures"), "--failure-threshold", "1",
    ])
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("watchdog must not restart services")),
    )
    assert mod.main() == 0
    assert guardian_seen == [Path("/home/board/codex-web-gpt-automation/bin/control_plane_guardian.py")]


def test_transient_failures_only_increment_counter(monkeypatch, tmp_path: Path):
    mod = load_module()
    monkeypatch.setattr(mod, "healthy", lambda url, timeout: False)
    monkeypatch.setattr(mod, "_run_guardian", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod, "_notify_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "argv", ["watchdog", "--state-path", str(tmp_path / "failures")])
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("watchdog must not restart services")),
    )
    state = tmp_path / "failures"
    assert mod.main() == 0 and state.read_text() == "1"
    assert mod.main() == 0 and state.read_text() == "2"
    assert mod.main() == 0 and state.read_text() == "3"


def test_healthy_watchdog_also_runs_progress_heartbeat(monkeypatch, tmp_path: Path):
    mod = load_module()
    seen: list[tuple[Path, Path, str]] = []
    guardian_seen: list[Path] = []
    monkeypatch.setattr(mod, "healthy", lambda url, timeout: True)
    monkeypatch.setattr(mod, "_run_guardian", lambda script: guardian_seen.append(script))
    monkeypatch.setattr(mod, "_notify_progress", lambda script, db, user: seen.append((script, db, user)))
    monkeypatch.setattr(sys, "argv", [
        "watchdog", "--state-path", str(tmp_path / "failures"),
        "--progress-script", str(tmp_path / "notify.py"),
        "--progress-db", str(tmp_path / "bus.sqlite3"),
    ])
    assert mod.main() == 0
    assert seen == [(tmp_path / "notify.py", tmp_path / "bus.sqlite3", "board")]
    assert guardian_seen == [Path("/home/board/codex-web-gpt-automation/bin/control_plane_guardian.py")]
