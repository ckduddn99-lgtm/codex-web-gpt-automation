#!/usr/bin/env python3
"""Root-only helper for narrowly allowlisted Project Control service recovery."""
from __future__ import annotations

import os
import subprocess
import sys

ALLOWED_ACTIONS = {"start", "restart", "reset-failed"}
ALLOWED_SERVICES = {
    "oracle-browser@board.service",
    "oracle-display@board.service",
    "oracle-window-manager@board.service",
    "oracle-vnc@board.service",
    "oracle-novnc@board.service",
    "chatgpt-server-worker@board.service",
    "codex-server-worker@board.service",
    "claude-server-worker@board.service",
    "gemini-server-worker@board.service",
    "board-gemini-bridge.service",
    "desktop-commander-remote.service",
    "tailscaled.service",
}


def main(argv: list[str]) -> int:
    if os.geteuid() != 0:
        print("project-control-ops must run as root", file=sys.stderr)
        return 77
    if len(argv) != 3:
        print("usage: project-control-ops <start|restart|reset-failed> <allowlisted.service>", file=sys.stderr)
        return 64
    action, service = argv[1], argv[2]
    if action not in ALLOWED_ACTIONS or service not in ALLOWED_SERVICES:
        print("requested operation is not allowlisted", file=sys.stderr)
        return 65
    proc = subprocess.run(
        ["/usr/bin/systemctl", action, service],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
