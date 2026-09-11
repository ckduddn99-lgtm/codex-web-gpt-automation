#!/usr/bin/env python3
"""Install the hardened control-plane systemd units using non-interactive sudo only."""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLS = (
    (ROOT / "bin/project_control_ops.py", Path("/usr/local/sbin/project-control-ops"), "0755"),
    (ROOT / "deploy/systemd/project-control-http.service", Path("/etc/systemd/system/project-control-http.service"), "0644"),
    (ROOT / "deploy/systemd/oracle-browser@.service", Path("/etc/systemd/system/oracle-browser@.service"), "0644"),
    (ROOT / "deploy/systemd/chatgpt-server-worker@.service", Path("/etc/systemd/system/chatgpt-server-worker@.service"), "0644"),
    (ROOT / "deploy/systemd/chatgpt-browser-demand.service", Path("/etc/systemd/system/chatgpt-browser-demand.service"), "0644"),
    (ROOT / "deploy/systemd/chatgpt-browser-demand.timer", Path("/etc/systemd/system/chatgpt-browser-demand.timer"), "0644"),
    (ROOT / "deploy/systemd/project-control-http-watchdog.service", Path("/etc/systemd/system/project-control-http-watchdog.service"), "0644"),
    (ROOT / "deploy/systemd/project-control-http-watchdog.timer", Path("/etc/systemd/system/project-control-http-watchdog.timer"), "0644"),
    (ROOT / "deploy/systemd/control-plane-guardian.service", Path("/etc/systemd/system/control-plane-guardian.service"), "0644"),
    (ROOT / "deploy/systemd/control-plane-guardian.timer", Path("/etc/systemd/system/control-plane-guardian.timer"), "0644"),
    (ROOT / "deploy/systemd/goal-progress-notify.service", Path("/etc/systemd/system/goal-progress-notify.service"), "0644"),
    (ROOT / "deploy/systemd/goal-progress-notify.timer", Path("/etc/systemd/system/goal-progress-notify.timer"), "0644"),
)
PROJECT_CONTROL = "project-control-http.service"
TIMERS = (
    "project-control-http-watchdog.timer",
    "control-plane-guardian.timer",
    "goal-progress-notify.timer",
    "chatgpt-browser-demand.timer",
)


def run(argv: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout, check=False)


def _http_healthy() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:7677/healthz", timeout=2.0) as response:
            return 200 <= int(response.status) < 300
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def main() -> int:
    steps: list[dict[str, object]] = []
    for source, target, mode in INSTALLS:
        proc = run(["/usr/bin/sudo", "-n", "/usr/bin/install", "-m", mode, str(source), str(target)])
        steps.append({"step": "install", "target": str(target), "exit_code": proc.returncode, "stderr": (proc.stderr or "")[-300:]})
        if proc.returncode != 0:
            print(json.dumps({"ok": False, "steps": steps}, ensure_ascii=False))
            return proc.returncode or 1
    proc = run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "daemon-reload"])
    steps.append({"step": "daemon-reload", "exit_code": proc.returncode, "stderr": (proc.stderr or "")[-300:]})
    if proc.returncode != 0:
        print(json.dumps({"ok": False, "steps": steps}, ensure_ascii=False))
        return proc.returncode or 1
    proc = run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "enable", PROJECT_CONTROL])
    steps.append({"step": "enable-project-control", "exit_code": proc.returncode, "stderr": (proc.stderr or "")[-300:]})
    if proc.returncode != 0:
        print(json.dumps({"ok": False, "steps": steps}, ensure_ascii=False))
        return proc.returncode or 1
    proc = run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "enable", "--now", *TIMERS], timeout=120)
    steps.append({"step": "enable-timers", "exit_code": proc.returncode, "stderr": (proc.stderr or "")[-500:]})
    if proc.returncode != 0:
        print(json.dumps({"ok": False, "steps": steps}, ensure_ascii=False))
        return proc.returncode or 1
    endpoint_healthy = _http_healthy()
    service_state = run(["/usr/bin/systemctl", "is-active", PROJECT_CONTROL])
    service_active = service_state.returncode == 0
    if not endpoint_healthy and not service_active:
        started = run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "start", PROJECT_CONTROL], timeout=60)
        steps.append({"step": "start-project-control", "exit_code": started.returncode, "stderr": (started.stderr or "")[-500:]})
        endpoint_healthy = _http_healthy()
        service_state = run(["/usr/bin/systemctl", "is-active", PROJECT_CONTROL])
        service_active = service_state.returncode == 0
    verify = run(["/usr/bin/systemctl", "is-active", *TIMERS])
    timer_states = [line.strip() for line in (verify.stdout or "").splitlines() if line.strip()]
    timers_ok = verify.returncode == 0 and len(timer_states) == len(TIMERS) and all(state == "active" for state in timer_states)
    ok = timers_ok and (service_active or endpoint_healthy)
    steps.append({"step": "verify", "project_control_service_active": service_active, "project_control_endpoint_healthy": endpoint_healthy, "timer_states": timer_states})
    print(json.dumps({"ok": ok, "steps": steps}, ensure_ascii=False))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
