#!/usr/bin/env python3
"""Claim Gemini-addressed bus tasks and answer them through Antigravity."""

from __future__ import annotations

import argparse
import os
import subprocess
import time
from pathlib import Path
from typing import Callable, Sequence

import chatgpt_server_bus as BUS


# See cli_server_worker: the question-versus-stage framing is the bus's, these are the
# constraints particular to an Antigravity seat held in plan mode.
TOOL_RULE = (
    "Do not call tools. Do not infer another participant's draft. "
    "Return only your answer."
)


def agy_argv(*, agy: Path, print_timeout: str) -> list[str]:
    return [
        str(agy),
        "--mode", "plan",
        "--output-format", "text",
        "--print-timeout", print_timeout,
    ]


def run_one(
    *,
    db_path: Path,
    recipient: str,
    worker_id: str,
    agy: Path,
    print_timeout: str = "5m",
    process_timeout: int = 420,
    provider_lock: Path | None = None,
    execute: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict:
    lock_path = provider_lock or Path(db_path).with_name("provider.lock")
    with BUS.provider_slot(lock_path) as acquired:
        if not acquired:
            return {"status": "busy"}
        return _run_one_locked(
            db_path=db_path, recipient=recipient, worker_id=worker_id, agy=agy,
            print_timeout=print_timeout, process_timeout=process_timeout, execute=execute,
        )


def _run_one_locked(
    *,
    db_path: Path,
    recipient: str,
    worker_id: str,
    agy: Path,
    print_timeout: str,
    process_timeout: int,
    execute: Callable[..., subprocess.CompletedProcess[str]],
) -> dict:
    task = BUS.claim(db_path, recipient=recipient, worker_id=worker_id)
    if task is None:
        return {"status": "idle"}
    task_id = int(task["task_id"])
    lease = task["lease_token"]
    inputs = BUS.task_inputs(
        db_path, task_id=task_id, recipient=recipient, lease_token=lease
    )
    lines = [
        f"{BUS.task_framing(task['stage'])} {TOOL_RULE}",
        (
            f"TASK {task_id} {task['from']}>{task['to']} {task['type']} "
            f"refs={','.join(str(ref) for ref in task['refs'])} priority={task['priority']}"
        ),
    ]
    for item in inputs:
        body = item["body"]
        lines.extend([
            f"QUESTION_BEGIN ref={item['ref']} chars={len(body)}",
            body,
            f"QUESTION_END ref={item['ref']}",
        ])
    packet = "\n".join(lines) + "\n"
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join([str(Path(agy).parent), env.get("PATH", "")])
    try:
        completed = execute(
            agy_argv(agy=agy, print_timeout=print_timeout),
            cwd=Path("/tmp") if os.name != "nt" else None,
            env=env,
            input=packet,
            text=True,
            capture_output=True,
            timeout=int(process_timeout),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return BUS.attention(
            db_path, task_id=task_id, recipient=recipient, lease_token=lease,
            error=f"Antigravity timed out; submission state is unknown: {exc}",
        )
    except OSError as exc:
        return BUS.attention(
            db_path, task_id=task_id, recipient=recipient, lease_token=lease,
            error=f"Antigravity could not start: {exc}",
        )
    answer = (completed.stdout or "").strip()
    if completed.returncode != 0 or not answer:
        detail = (completed.stderr or completed.stdout or "Antigravity returned no answer")[-2000:]
        return BUS.attention(
            db_path, task_id=task_id, recipient=recipient, lease_token=lease,
            error=f"Antigravity did not prove completion; no automatic retry. {detail}",
        )
    return BUS.complete(
        db_path, task_id=task_id, recipient=recipient, lease_token=lease,
        result=answer + "\n",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--recipient", default="gemini")
    parser.add_argument("--worker-id", default="gemini-antigravity")
    parser.add_argument("--agy", type=Path, default=Path.home() / ".local/bin/agy")
    parser.add_argument("--print-timeout", default="5m")
    parser.add_argument("--process-timeout", type=int, default=420)
    parser.add_argument("--provider-lock", type=Path)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--interval", type=float, default=5.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    while True:
        try:
            print(run_one(
                db_path=args.db,
                recipient=args.recipient,
                worker_id=args.worker_id,
                agy=args.agy,
                print_timeout=args.print_timeout,
                process_timeout=args.process_timeout,
                provider_lock=args.provider_lock,
            ), flush=True)
        except BUS.BusError as exc:
            print({"status": "attention_required", "code": exc.code, "error": str(exc)}, flush=True)
        if not args.serve:
            return 0
        time.sleep(max(1.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
