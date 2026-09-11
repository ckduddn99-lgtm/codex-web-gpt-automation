#!/usr/bin/env python3
"""Start the heavyweight ChatGPT browser only when pending work needs it."""
from __future__ import annotations

import argparse
import fcntl
import json
import socket
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

DEFAULT_DB = Path("/home/board/.local/state/ai-bus/bus.sqlite3")
STATE_PATH = Path("/run/chatgpt-browser-demand/state.json")
LOCK_PATH = Path("/run/chatgpt-browser-demand/controller.lock")
BROWSER = "oracle-browser@board.service"
WORKER = "chatgpt-server-worker@board.service"
ALLOWED = {BROWSER, WORKER}
MIN_AVAILABLE = 420 * 1024 * 1024
MAX_IO_PSI = 10.0
MAX_MEMORY_PSI = 5.0


def _systemctl(action: str, service: str, timeout: float = 45.0) -> bool:
    if action not in {"start", "stop", "is-active"} or service not in ALLOWED:
        raise ValueError("operation is not allowlisted")
    try:
        proc = subprocess.run(
            ["/usr/bin/systemctl", action, service],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout, check=False,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _active(service: str) -> bool:
    return _systemctl("is-active", service, timeout=5.0)


def _pending(db_path: Path) -> int:
    if not db_path.is_file():
        return 0
    try:
        with sqlite3.connect(db_path, timeout=2.0) as db:
            row = db.execute("SELECT COUNT(*) FROM tasks WHERE recipient = 'chatgpt' AND status = 'pending'").fetchone()
        return int(row[0]) if row else 0
    except (sqlite3.Error, OSError, ValueError):
        return 0


def _psi(kind: str) -> float | None:
    try:
        line = next(row for row in Path(f"/proc/pressure/{kind}").read_text(encoding="utf-8").splitlines() if row.startswith("full "))
        fields = dict(item.split("=", 1) for item in line.split()[1:] if "=" in item)
        return float(fields["avg10"])
    except (OSError, StopIteration, KeyError, ValueError):
        return None


def _available_memory() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def pressure_snapshot() -> dict[str, Any]:
    return {
        "memory_available": _available_memory(),
        "io_psi_full_avg10": _psi("io"),
        "memory_psi_full_avg10": _psi("memory"),
    }


def safe_to_start(snapshot: dict[str, Any]) -> bool:
    available = snapshot.get("memory_available")
    io_psi = snapshot.get("io_psi_full_avg10")
    mem_psi = snapshot.get("memory_psi_full_avg10")
    return bool(
        isinstance(available, int) and available >= MIN_AVAILABLE
        and (io_psi is None or float(io_psi) < MAX_IO_PSI)
        and (mem_psi is None or float(mem_psi) < MAX_MEMORY_PSI)
    )


def _cdp_ready(timeout: float = 45.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", 9222), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def _load_state() -> dict[str, Any]:
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_name(".state.json.tmp")
    tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_PATH)


def run_once(db_path: Path = DEFAULT_DB) -> dict[str, Any]:
    pending = _pending(db_path)
    worker_active = _active(WORKER)
    browser_active = _active(BROWSER)
    state = _load_state()
    managed_browser = bool(state.get("managed_browser"))
    pressure = pressure_snapshot()
    actions: list[dict[str, Any]] = []

    if pending <= 0:
        if managed_browser and browser_active and not worker_active:
            stopped = _systemctl("stop", BROWSER)
            actions.append({"service": BROWSER, "action": "idle-stop", "ok": stopped})
            if stopped:
                managed_browser = False
        state.update({"managed_browser": managed_browser, "last_pending": 0, "updated_at": time.time()})
        _save_state(state)
        return {"ok": True, "pending": 0, "actions": actions, "pressure": pressure}

    if worker_active:
        return {"ok": True, "pending": pending, "actions": actions, "pressure": pressure}

    if not safe_to_start(pressure):
        actions.append({"service": BROWSER, "action": "start-deferred-pressure", "ok": True})
        return {"ok": True, "pending": pending, "actions": actions, "pressure": pressure}

    if not browser_active:
        started = _systemctl("start", BROWSER)
        actions.append({"service": BROWSER, "action": "start", "ok": started})
        if not started:
            return {"ok": False, "pending": pending, "actions": actions, "pressure": pressure}
        managed_browser = True
        state.update({"managed_browser": True, "updated_at": time.time()})
        _save_state(state)

    if not _cdp_ready():
        if managed_browser:
            stopped = _systemctl("stop", BROWSER)
            actions.append({"service": BROWSER, "action": "cdp-timeout-stop", "ok": stopped})
            if stopped:
                managed_browser = False
        state.update({"managed_browser": managed_browser, "updated_at": time.time()})
        _save_state(state)
        return {"ok": False, "pending": pending, "actions": actions, "pressure": pressure}

    started_worker = _systemctl("start", WORKER)
    actions.append({"service": WORKER, "action": "start", "ok": started_worker})
    state.update({"managed_browser": managed_browser, "last_pending": pending, "updated_at": time.time()})
    _save_state(state)
    return {"ok": started_worker, "pending": pending, "actions": actions, "pressure": pressure}


def status(db_path: Path = DEFAULT_DB) -> dict[str, Any]:
    state = _load_state()
    return {
        "schema": "chatgpt-browser-demand/v1",
        "pending": _pending(db_path),
        "browser_active": _active(BROWSER),
        "worker_active": _active(WORKER),
        "managed_browser": bool(state.get("managed_browser")),
        "pressure": pressure_snapshot(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.status:
        print(json.dumps(status(args.db), ensure_ascii=False, sort_keys=True))
        return 0

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        result = run_once(args.db)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
