#!/usr/bin/env python3
"""Restart the Project Control HTTP adapter when its local health endpoint stalls."""
from __future__ import annotations

import argparse
import subprocess
import urllib.error
import urllib.request


def healthy(url: str, timeout: float) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= int(response.status) < 300
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:7677/healthz")
    parser.add_argument("--service", default="project-control-http.service")
    parser.add_argument("--timeout", type=float, default=3.0)
    args = parser.parse_args()
    if healthy(args.url, args.timeout):
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
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
