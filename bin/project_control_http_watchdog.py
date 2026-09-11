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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:7677/healthz")
    parser.add_argument("--service", default="project-control-http.service")
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--failure-threshold", type=int, default=3)
    parser.add_argument("--state-path", type=Path, default=Path("/run/project-control-http-watchdog.failures"))
    args = parser.parse_args()
    if args.failure_threshold < 1:
        parser.error("--failure-threshold must be at least 1")
    if healthy(args.url, args.timeout):
        _write_failures(args.state_path, 0)
        return 0
    failures = _read_failures(args.state_path) + 1
    _write_failures(args.state_path, failures)
    if failures < args.failure_threshold:
        return 0
    completed = subprocess.run(
        ["/usr/bin/systemctl", "restart", args.service],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode == 0:
        _write_failures(args.state_path, 0)
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
