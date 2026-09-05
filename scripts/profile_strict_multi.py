"""Measure real Git subprocesses without substituting fixture or validation work."""
from __future__ import annotations

import collections
import functools
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    original = subprocess.run
    samples = collections.defaultdict(list)
    lock = threading.Lock()

    def measured(*args, **kwargs):
        command = args[0] if args else kwargs.get("args", [])
        start = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            if isinstance(command, (list, tuple)) and Path(str(command[0])).stem == "git":
                parts = list(command[1:])
                if parts[:1] == ["-C"]:
                    parts = parts[2:]
                while parts[:1] == ["-c"]:
                    parts = parts[2:]
                frame = sys._getframe(1)
                phase = "other"
                while frame is not None:
                    if frame.f_code.co_name in {"make_strict_manifest", "_create_strict_repository", "load_manifest", "_strict_preflight", "_strict_audit_lane", "_strict_repository_barrier"}:
                        phase = frame.f_code.co_name
                        break
                    frame = frame.f_back
                name = "git " + " ".join(parts[:2] if parts[:1] == ["worktree"] else parts[:1])
                elapsed = time.perf_counter() - start
                with lock:
                    samples[name].append(elapsed)
                    samples[f"phase {phase} / {name}"].append(elapsed)

    def timed(name, function):
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            start = time.perf_counter()
            try:
                return function(*args, **kwargs)
            finally:
                with lock:
                    samples[name].append(time.perf_counter() - start)
        return wrapped

    class Profile:
        def pytest_collection_modifyitems(self, items):
            seen = set()
            for item in items:
                module = item.module
                if module in seen or not hasattr(module, "make_strict_manifest"):
                    continue
                seen.add(module)
                factory = module.make_strict_manifest

                def measured_factory(*args, **kwargs):
                    start = time.perf_counter()
                    try:
                        return factory(*args, **kwargs)
                    finally:
                        samples["make_strict_manifest"].append(time.perf_counter() - start)

                module.make_strict_manifest = measured_factory
                original_load = module.load

                def measured_load():
                    loaded = original_load()
                    for name in ("load_manifest", "_strict_preflight", "_strict_audit_lane", "_strict_repository_barrier"):
                        setattr(loaded, name, timed(name, getattr(loaded, name)))
                    return loaded

                module.load = measured_load

    temp_parent = Path(tempfile.gettempdir()) / "Codex"
    temp_parent.mkdir(exist_ok=True)
    start = time.perf_counter()
    subprocess.run = measured
    try:
        with tempfile.TemporaryDirectory(prefix="strict-profile-", dir=temp_parent) as temporary:
            result = pytest.main([
                "-q", "-p", "no:cacheprovider", str(ROOT / "tests/test_chatgpt_oracle_multi.py"),
                "--basetemp", temporary, "--durations=12",
            ], plugins=[Profile()])
            cleanup_start = time.perf_counter()
        cleanup = time.perf_counter() - cleanup_start
    finally:
        subprocess.run = original
    for name, values in sorted(samples.items()):
        print(f"PROFILE {name}: calls={len(values)} wall={sum(values):.3f}s max={max(values):.3f}s")
    print(f"PROFILE cleanup={cleanup:.3f}s total={time.perf_counter() - start:.3f}s")
    return int(result)


if __name__ == "__main__":
    raise SystemExit(main())
