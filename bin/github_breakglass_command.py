#!/usr/bin/env python3
"""Forced-command endpoint for ephemeral GitHub Actions break-glass SSH.

The SSH key using this command never receives a shell. Only four fixed tokens are
accepted and each maps to bounded, allowlisted local recovery operations.
"""
from __future__ import annotations

import os
import subprocess
import sys
import urllib.error
import urllib.request

OPS = "/usr/local/sbin/project-control-ops"
PROJECT_CONTROL = "project-control-http.service"
TAILSCALE = "tailscaled.service"
BROWSER = "oracle-browser@board.service"
WORKER = "chatgpt-server-worker@board.service"


def run(argv: list[str], timeout: int = 60) -> int:
    proc = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
    )
    sys.stdout.write((proc.stdout or "")[-4000:])
    return int(proc.returncode)


def healthy() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:7677/healthz", timeout=3.0) as response:
            return 200 <= int(response.status) < 300
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def service(action: str, unit: str) -> int:
    return run(["/usr/bin/sudo", "-n", OPS, action, unit])


def status() -> int:
    rc = run([
        "/usr/bin/systemctl", "is-active",
        PROJECT_CONTROL, "project-control-http-watchdog.timer",
        "control-plane-guardian.timer", TAILSCALE, BROWSER, WORKER,
    ])
    print(f"healthz={'ok' if healthy() else 'failed'}")
    return 0 if healthy() else (rc or 2)


def heal() -> int:
    service("stop", WORKER)
    service("stop", BROWSER)
    rc_pc = 0 if healthy() else service("restart", PROJECT_CONTROL)
    rc_ts = service("restart", TAILSCALE)
    print(f"project_control={'ok' if healthy() else 'failed'} tailscale_rc={rc_ts}")
    return 0 if healthy() and rc_pc == 0 else 2


def main() -> int:
    command = os.environ.get("SSH_ORIGINAL_COMMAND", "").strip()
    if command == "/status":
        return status()
    if command == "/heal":
        return heal()
    if command == "/restart_pc":
        rc = service("restart", PROJECT_CONTROL)
        print(f"healthz={'ok' if healthy() else 'failed'}")
        return 0 if rc == 0 and healthy() else 2
    if command == "/stop_browser":
        rc1 = service("stop", WORKER)
        rc2 = service("stop", BROWSER)
        return 0 if rc1 == 0 and rc2 == 0 else 2
    print("unsupported break-glass command", file=sys.stderr)
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
