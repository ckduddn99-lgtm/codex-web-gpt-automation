#!/usr/bin/env python3
"""Independent, fail-closed recovery guardian for Project Control connectivity."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

STATE_PATH = Path("/run/control-plane-guardian/state.json")
PROJECT_CONTROL_URL = "http://127.0.0.1:7677/healthz"
PROJECT_CONTROL_SERVICE = "project-control-http.service"
COMMANDER_SERVICE = "desktop-commander-remote.service"
TAILSCALE_SERVICE = "tailscaled.service"
BROWSER_SERVICE = "oracle-browser@board.service"
BROWSER_WORKER_SERVICE = "chatgpt-server-worker@board.service"
ALLOWED_SERVICES = {
    PROJECT_CONTROL_SERVICE, COMMANDER_SERVICE, TAILSCALE_SERVICE,
    BROWSER_SERVICE, BROWSER_WORKER_SERVICE,
}
CLEANUP_SCRIPT = Path("/home/board/codex-web-gpt-automation/bin/project_host_cleanup.py")
BUS_DB = Path("/home/board/.local/state/ai-bus/bus.sqlite3")
CLEANUP_USER = "ckduddn99"


def _load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    temp.replace(path)


def _active(service: str, timeout: float = 4.0) -> bool:
    if service not in ALLOWED_SERVICES:
        raise ValueError("service is not allowlisted")
    try:
        proc = subprocess.run(
            ["/usr/bin/systemctl", "is-active", "--quiet", service],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout, check=False,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _healthy(url: str = PROJECT_CONTROL_URL, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= int(response.status) < 300
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def _restart(service: str, timeout: float = 30.0) -> bool:
    if service not in ALLOWED_SERVICES:
        raise ValueError("service is not allowlisted")
    try:
        proc = subprocess.run(
            ["/usr/bin/systemctl", "restart", service],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout, check=False,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _stop(service: str, timeout: float = 30.0) -> bool:
    if service not in ALLOWED_SERVICES:
        raise ValueError("service is not allowlisted")
    try:
        proc = subprocess.run(
            ["/usr/bin/systemctl", "stop", service],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout, check=False,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        lines = Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        key, sep, raw = line.partition(":")
        if sep and raw.strip().split() and raw.strip().split()[0].isdigit():
            values[key] = int(raw.strip().split()[0]) * 1024
    return values


def _psi(kind: str) -> float | None:
    try:
        line = next(
            row for row in Path(f"/proc/pressure/{kind}").read_text(encoding="utf-8").splitlines()
            if row.startswith("full ")
        )
        fields = dict(item.split("=", 1) for item in line.split()[1:] if "=" in item)
        return float(fields["avg10"])
    except (OSError, StopIteration, KeyError, ValueError):
        return None


def pressure_snapshot() -> dict[str, Any]:
    mem = _meminfo()
    cpu_count = os.cpu_count() or 1
    try:
        load1 = float(os.getloadavg()[0])
    except OSError:
        load1 = 0.0
    return {
        "cpu_count": cpu_count,
        "load1": round(load1, 2),
        "memory_available": mem.get("MemAvailable"),
        "swap_free": mem.get("SwapFree"),
        "swap_total": mem.get("SwapTotal"),
        "memory_psi_full_avg10": _psi("memory"),
        "io_psi_full_avg10": _psi("io"),
    }


def critical_pressure(snapshot: dict[str, Any]) -> bool:
    available = snapshot.get("memory_available")
    swap_free = snapshot.get("swap_free")
    swap_total = snapshot.get("swap_total")
    load1 = float(snapshot.get("load1") or 0.0)
    cpus = max(1, int(snapshot.get("cpu_count") or 1))
    mem_psi = snapshot.get("memory_psi_full_avg10")
    io_psi = snapshot.get("io_psi_full_avg10")
    low_memory = isinstance(available, int) and available < 192 * 1024 * 1024
    low_swap = isinstance(swap_free, int) and isinstance(swap_total, int) and swap_total > 0 and swap_free < 256 * 1024 * 1024
    severe_stall = (isinstance(mem_psi, (int, float)) and mem_psi >= 20.0) or (isinstance(io_psi, (int, float)) and io_psi >= 20.0)
    overloaded = load1 >= cpus * 4.0
    return bool((low_memory and low_swap) or (severe_stall and (low_memory or overloaded)))


def emergency_pressure(snapshot: dict[str, Any]) -> bool:
    """Return true only when the host is close to control-plane starvation."""
    available = snapshot.get("memory_available")
    mem_psi = snapshot.get("memory_psi_full_avg10")
    io_psi = snapshot.get("io_psi_full_avg10")
    very_low_memory = isinstance(available, int) and available < 128 * 1024 * 1024
    memory_stall = isinstance(mem_psi, (int, float)) and mem_psi >= 40.0
    io_stall = isinstance(io_psi, (int, float)) and io_psi >= 50.0
    return bool(very_low_memory or memory_stall or io_stall)


def _cleanup(timeout: float = 210.0) -> dict[str, Any]:
    if not CLEANUP_SCRIPT.is_file() or not BUS_DB.is_file():
        return {"attempted": False, "reason": "cleanup_inputs_missing"}
    command = ["/usr/bin/python3", str(CLEANUP_SCRIPT), "--db", str(BUS_DB), "--reclaim-only"]
    runuser = Path("/usr/sbin/runuser")
    if runuser.is_file():
        command = [str(runuser), "-u", CLEANUP_USER, "--", *command]
    try:
        proc = subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout, check=False,
        )
        return {"attempted": True, "exit_code": int(proc.returncode), "ok": proc.returncode == 0}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"attempted": True, "ok": False, "reason": str(exc)[:300]}


def run_once(*, state_path: Path = STATE_PATH, health_failure_threshold: int = 3,
             cleanup_cooldown: float = 900.0, now: float | None = None) -> dict[str, Any]:
    if health_failure_threshold < 1:
        raise ValueError("health_failure_threshold must be >= 1")
    current_time = time.time() if now is None else float(now)
    state = _load_state(state_path)
    failures = dict(state.get("failures") or {})
    actions: list[dict[str, Any]] = []

    for service in (TAILSCALE_SERVICE, COMMANDER_SERVICE):
        if _active(service):
            failures[service] = 0
            continue
        failures[service] = int(failures.get(service) or 0) + 1
        restarted = _restart(service)
        actions.append({"service": service, "action": "restart", "ok": restarted})
        if restarted:
            failures[service] = 0

    pressure = pressure_snapshot()
    pressured = critical_pressure(pressure)
    emergency = emergency_pressure(pressure)
    cleanup: dict[str, Any] | None = None

    # The browser is explicitly disposable. Under extreme host pressure, stop
    # its always-on worker first and then Chrome even if a durable provider run
    # is active. The run can recover later; the control plane must remain alive.
    if emergency:
        for service in (BROWSER_WORKER_SERVICE, BROWSER_SERVICE):
            if _active(service):
                stopped = _stop(service)
                actions.append({"service": service, "action": "emergency-stop", "ok": stopped})
    last_cleanup = float(state.get("last_cleanup") or 0.0)
    if pressured and current_time - last_cleanup >= cleanup_cooldown:
        cleanup = _cleanup()
        if cleanup.get("attempted"):
            state["last_cleanup"] = current_time

    pc_active = _active(PROJECT_CONTROL_SERVICE)
    pc_healthy = _healthy()
    if pc_healthy:
        failures[PROJECT_CONTROL_SERVICE] = 0
    else:
        misses = int(failures.get(PROJECT_CONTROL_SERVICE) or 0) + 1
        failures[PROJECT_CONTROL_SERVICE] = misses
        if not pc_active:
            restarted = _restart(PROJECT_CONTROL_SERVICE)
            verified = restarted and _healthy()
            actions.append({"service": PROJECT_CONTROL_SERVICE, "action": "restart", "ok": verified, "misses": misses})
            if verified:
                failures[PROJECT_CONTROL_SERVICE] = 0
        elif misses >= health_failure_threshold and not pressured:
            restarted = _restart(PROJECT_CONTROL_SERVICE)
            verified = restarted and _healthy()
            actions.append({"service": PROJECT_CONTROL_SERVICE, "action": "restart", "ok": verified, "misses": misses})
            if verified:
                failures[PROJECT_CONTROL_SERVICE] = 0
        elif pressured:
            actions.append({"service": PROJECT_CONTROL_SERVICE, "action": "restart-deferred-pressure", "ok": True, "misses": misses})

    state["failures"] = failures
    state["updated_at"] = current_time
    _save_state(state_path, state)
    return {
        "schema": "project-control.guardian/v1",
        "ok": not any(item.get("ok") is False for item in actions),
        "actions": actions,
        "pressure": pressure,
        "critical_pressure": pressured,
        "emergency_pressure": emergency,
        "cleanup": cleanup,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-path", type=Path, default=STATE_PATH)
    parser.add_argument("--health-failure-threshold", type=int, default=3)
    parser.add_argument("--cleanup-cooldown", type=float, default=900.0)
    args = parser.parse_args()
    result = run_once(
        state_path=args.state_path,
        health_failure_threshold=args.health_failure_threshold,
        cleanup_cooldown=args.cleanup_cooldown,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
