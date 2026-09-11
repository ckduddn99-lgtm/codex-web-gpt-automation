#!/usr/bin/env python3
"""Restart the Project Control HTTP adapter when its local health endpoint stalls."""
from __future__ import annotations

import argparse
import subprocess
import urllib.error
import urllib.request
from pathlib import Path


def healthy(url: str, timeout: float) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= int(response.status) < 300
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def _read_failures(path: Path) -> int:
    try:
        return max(0, int(path.read_text(encoding="utf-8").strip()))
    except (OSError, ValueError):
        return 0


def _write_failures(path: Path, failures: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(str(max(0, failures)), encoding="utf-8")
    temporary.replace(path)


def _run_guardian(script: Path, timeout: float = 220.0) -> None:
    if not script.is_file():
        return
    try:
        subprocess.run(
            ["/usr/bin/python3", str(script)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return


def _notify_progress(script: Path, db: Path, user: str = "board", timeout: float = 20.0) -> None:
    if not script.is_file() or not db.is_file():
        return
    command = ["/usr/bin/python3", str(script), "--db", str(db), "--channel", "일반"]
    runuser = Path("/usr/sbin/runuser")
    if user and runuser.is_file():
        command = [str(runuser), "-u", user, "--", *command]
    try:
        subprocess.run(
            command,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:7677/healthz")
    parser.add_argument("--service", default="project-control-http.service")
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--failure-threshold", type=int, default=3)
    parser.add_argument("--state-path", type=Path, default=Path("/run/project-control-http-watchdog.failures"))
    parser.add_argument("--guardian-script", type=Path, default=Path("/home/board/codex-web-gpt-automation/bin/control_plane_guardian.py"))
    parser.add_argument("--progress-script", type=Path, default=Path("/home/board/codex-web-gpt-automation/bin/goal_progress_notify.py"))
    parser.add_argument("--progress-db", type=Path, default=Path("/home/board/.local/state/ai-bus/bus.sqlite3"))
    parser.add_argument("--progress-user", default="board")
    args = parser.parse_args()
    if args.failure_threshold < 1:
        parser.error("--failure-threshold must be at least 1")

    result = 0
    if healthy(args.url, args.timeout):
        _write_failures(args.state_path, 0)
    else:
        failures = _read_failures(args.state_path) + 1
        _write_failures(args.state_path, failures)
        if failures >= args.failure_threshold:
            completed = subprocess.run(
                ["/usr/bin/systemctl", "restart", args.service],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
            )
            result = int(completed.returncode)
            if completed.returncode == 0:
                _write_failures(args.state_path, 0)
    _run_guardian(args.guardian_script)
    _notify_progress(args.progress_script, args.progress_db, args.progress_user)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
