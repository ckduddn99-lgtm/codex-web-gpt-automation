#!/usr/bin/env python3
"""Bounded, read-only host health probe for Project Control incident diagnosis."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

ENDPOINTS = (
    ("github", "api.github.com", 443),
    ("openai", "api.openai.com", 443),
    ("chatgpt", "chatgpt.com", 443),
    ("anthropic", "api.anthropic.com", 443),
    ("gemini", "generativelanguage.googleapis.com", 443),
    ("commander", "mcp.desktopcommander.app", 443),
)


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _int_file(path: str) -> int | None:
    raw = _read(path)
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


def _meminfo() -> dict[str, int]:
    result: dict[str, int] = {}
    raw = _read("/proc/meminfo") or ""
    for line in raw.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        first = value.strip().split()[0] if value.strip() else ""
        if first.isdigit():
            result[key] = int(first) * 1024
    return result


def _tcp_states(path: str) -> dict[str, int]:
    states: dict[str, int] = {}
    raw = _read(path) or ""
    for line in raw.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        state = parts[3]
        states[state] = states.get(state, 0) + 1
    return states


def _resolve_ipv4(host: str) -> tuple[str | None, str | None, float]:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            ["getent", "ahostsv4", host],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, type(exc).__name__, round((time.monotonic() - started) * 1000, 1)
    if proc.returncode != 0:
        return None, f"getent-exit-{proc.returncode}", round((time.monotonic() - started) * 1000, 1)
    for line in proc.stdout.splitlines():
        value = line.split()[0] if line.split() else ""
        try:
            socket.inet_aton(value)
        except OSError:
            continue
        return value, None, round((time.monotonic() - started) * 1000, 1)
    return None, "no-ipv4", round((time.monotonic() - started) * 1000, 1)


def _probe(name: str, host: str, port: int) -> dict[str, object]:
    ip, dns_error, dns_ms = _resolve_ipv4(host)
    result: dict[str, object] = {"name": name, "host": host, "dns_ms": dns_ms, "dns_ok": ip is not None}
    if ip is None:
        result.update({"tcp_ok": False, "error": dns_error})
        return result
    started = time.monotonic()
    try:
        with socket.create_connection((ip, port), timeout=1.5):
            result.update({"tcp_ok": True, "tcp_ms": round((time.monotonic() - started) * 1000, 1)})
    except OSError as exc:
        result.update({"tcp_ok": False, "tcp_ms": round((time.monotonic() - started) * 1000, 1), "error": type(exc).__name__})
    return result


def _top_processes() -> list[dict[str, object]]:
    """Return a bounded /proc snapshot even when spawning `ps` is unhealthy."""
    try:
        ticks_per_second = int(os.sysconf("SC_CLK_TCK"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
    except (OSError, ValueError):
        ticks_per_second, page_size = 100, 4096

    def sample() -> dict[int, dict[str, object]]:
        rows: dict[int, dict[str, object]] = {}
        try:
            proc_entries = list(Path("/proc").iterdir())
        except OSError:
            return rows
        for entry in proc_entries:
            if not entry.name.isdigit():
                continue
            try:
                raw = (entry / "stat").read_text(encoding="utf-8")
                close = raw.rfind(")")
                if close < 0:
                    continue
                pid = int(raw[:raw.find(" ")])
                comm = raw[raw.find("(") + 1:close]
                fields = raw[close + 2:].split()
                if len(fields) < 22:
                    continue
                cmdline_raw = (entry / "cmdline").read_bytes()[:1024]
                cmdline = cmdline_raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
                rows[pid] = {
                    "pid": pid,
                    "ppid": int(fields[1]),
                    "stat": fields[0],
                    "cpu_ticks": int(fields[11]) + int(fields[12]),
                    "start_ticks": int(fields[19]),
                    "rss_pages": int(fields[21]),
                    "comm": comm,
                    "args": (cmdline or comm)[:240],
                }
            except (OSError, ValueError, IndexError):
                continue
        return rows

    first = sample()
    started = time.monotonic()
    time.sleep(0.25)
    second = sample()
    elapsed = max(time.monotonic() - started, 0.001)
    try:
        uptime = float((_read("/proc/uptime") or "0").split()[0])
    except (ValueError, IndexError):
        uptime = 0.0
    mem_total = _meminfo().get("MemTotal") or 0
    rows: list[dict[str, object]] = []
    for pid, current in second.items():
        previous = first.get(pid)
        if previous is None:
            continue
        delta_ticks = max(int(current["cpu_ticks"]) - int(previous["cpu_ticks"]), 0)
        cpu_pct = delta_ticks / ticks_per_second / elapsed * 100.0
        rss_bytes = int(current["rss_pages"]) * page_size
        start_s = int(current["start_ticks"]) / ticks_per_second
        rows.append({
            "pid": pid,
            "ppid": current["ppid"],
            "stat": current["stat"],
            "cpu_pct": round(cpu_pct, 1),
            "cpu_total_s": round(int(current["cpu_ticks"]) / ticks_per_second, 1),
            "mem_pct": round((rss_bytes / mem_total * 100.0) if mem_total else 0.0, 1),
            "rss_kib": rss_bytes // 1024,
            "elapsed_s": max(int(uptime - start_s), 0),
            "comm": current["comm"],
            "args": current["args"],
        })
    rows.sort(key=lambda item: (float(item["cpu_pct"]), int(item["rss_kib"])), reverse=True)
    return rows[:12]


def main() -> int:
    mem = _meminfo()
    disk = shutil.disk_usage("/")
    load = os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)
    proc_count = sum(1 for item in Path("/proc").iterdir() if item.name.isdigit())
    payload = {
        "schema": "project-control.host-health/v1",
        "cpu_count": os.cpu_count(),
        "loadavg": [round(value, 2) for value in load],
        "memory": {"total": mem.get("MemTotal"), "available": mem.get("MemAvailable"), "swap_total": mem.get("SwapTotal"), "swap_free": mem.get("SwapFree")},
        "disk_root": {"total": disk.total, "free": disk.free},
        "process_count": proc_count,
        "top_processes": _top_processes(),
        "file_nr": _read("/proc/sys/fs/file-nr"),
        "conntrack": {"count": _int_file("/proc/sys/net/netfilter/nf_conntrack_count"), "max": _int_file("/proc/sys/net/netfilter/nf_conntrack_max")},
        "ephemeral_port_range": _read("/proc/sys/net/ipv4/ip_local_port_range"),
        "tcp4_states": _tcp_states("/proc/net/tcp"),
        "tcp6_states": _tcp_states("/proc/net/tcp6"),
        "endpoints": [_probe(*item) for item in ENDPOINTS],
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
