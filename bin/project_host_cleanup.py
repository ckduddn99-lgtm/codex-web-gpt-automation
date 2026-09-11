#!/usr/bin/env python3
"""Reclaim stale Oracle resources only when no durable provider run is active."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import socket
import sqlite3
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT.parent / ".local/state/ai-bus/bus.sqlite3"
ROOT_OPS_HELPER = "/usr/local/sbin/project-control-ops"
BROWSER_SERVICE = "oracle-browser@board.service"


def _meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        lines = Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        key, sep, raw = line.partition(":")
        if not sep:
            continue
        first = raw.strip().split()[0] if raw.strip() else ""
        if first.isdigit():
            values[key] = int(first) * 1024
    return values


def _snapshot() -> dict[str, object]:
    mem = _meminfo()
    return {
        "loadavg": [round(value, 2) for value in os.getloadavg()],
        "memory_available": mem.get("MemAvailable"),
        "swap_free": mem.get("SwapFree"),
        "swap_total": mem.get("SwapTotal"),
    }


def _running_rows(db_path: Path) -> list[dict[str, object]]:
    if not db_path.is_file():
        raise RuntimeError(f"bus database is unavailable: {db_path}")
    rows: list[dict[str, object]] = []
    with sqlite3.connect(db_path) as db:
        for table in ("goal_task_runs", "goal_driver_runs"):
            for run_id, started_at in db.execute(
                f"SELECT id, started_at FROM {table} WHERE status = 'running' ORDER BY id"
            ):
                rows.append({"table": table, "id": int(run_id), "started_at": started_at})
    return rows


def _cmdline(pid: int) -> str:
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()[:8192]
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()


def _stale_oracle_pids() -> list[int]:
    pids: list[int] = []
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return pids
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        command = _cmdline(pid)
        if "goal-task-" not in command:
            continue
        if "--slug" not in command and "/goal-runs/goal-task-" not in command:
            continue
        pids.append(pid)
    return sorted(pids)


def _terminate(pids: list[int]) -> dict[str, list[int]]:
    sent: list[int] = []
    killed: list[int] = []
    denied: list[int] = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            sent.append(pid)
        except ProcessLookupError:
            pass
        except PermissionError:
            denied.append(pid)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if not any(Path(f"/proc/{pid}").exists() for pid in sent):
            break
        time.sleep(0.1)
    for pid in sent:
        if not Path(f"/proc/{pid}").exists():
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            killed.append(pid)
        except ProcessLookupError:
            pass
        except PermissionError:
            if pid not in denied:
                denied.append(pid)
    return {"term_sent": sent, "kill_sent": killed, "permission_denied": denied}


def _cdp_ready(timeout: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", 9222), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--reclaim-only", action="store_true", help="Stop the browser after reclaim instead of restarting it.")
    args = parser.parse_args()
    db_path = args.db.expanduser().resolve()
    lock_path = db_path.with_name("provider.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"schema": "project-control.host-cleanup/v1", "ok": False, "reason": "provider_busy"}))
            return 2

        running = _running_rows(db_path)
        if running:
            print(json.dumps({
                "schema": "project-control.host-cleanup/v1",
                "ok": False,
                "reason": "provider_runs_active",
                "running": running,
            }))
            return 3

        before = _snapshot()
        stale = _stale_oracle_pids()
        terminated = _terminate(stale)
        action = "stop" if args.reclaim_only else "restart"
        browser = subprocess.run(
            ["/usr/bin/sudo", "-n", ROOT_OPS_HELPER, action, BROWSER_SERVICE],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=150,
            check=False,
        )
        ready = browser.returncode == 0 and (args.reclaim_only or _cdp_ready())
        time.sleep(1.0)
        payload = {
            "schema": "project-control.host-cleanup/v1",
            "ok": bool(ready),
            "before": before,
            "after": _snapshot(),
            "stale_oracle_pids": stale,
            "terminated": terminated,
            "browser_action": action,
            "browser_action_exit_code": int(browser.returncode),
            "browser_cdp_ready": False if args.reclaim_only else bool(ready),
            "stderr": (browser.stderr or "")[-500:],
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0 if ready else 4


if __name__ == "__main__":
    raise SystemExit(main())
