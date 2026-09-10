#!/usr/bin/env python3
"""Claim ChatGPT-addressed bus tasks and answer them in ordinary ChatGPT web chats."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Sequence

import chatgpt_server_bus as BUS


ORACLE_PACKAGE = "@steipete/oracle@0.18.0"
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_NPX = REPO_ROOT / "runtime/node-current/bin/npx"
# See cli_server_worker: the question-versus-stage framing is the bus's.
TOOL_RULE = (
    "Do not invent, request, or infer another participant's draft. "
    "Return only your own substantive answer."
)


class WorkerError(RuntimeError):
    pass


def oracle_argv(
    *, npx: Path, profile: Path, packet: Path, output: Path, task_id: int, timeout: str,
    model: str, stage: str
) -> list[str]:
    return [
        str(npx), "--yes", ORACLE_PACKAGE,
        "--engine", "browser",
        "--copy-profile", str(profile),
        "--model", model,
        "--browser-model-strategy", "select",
        "--browser-archive", "never",
        "--timeout", timeout,
        "--no-notify",
        "--slug", f"server-bus-task-{int(task_id)}",
        "--prompt", f"{BUS.task_framing(stage)} {TOOL_RULE}",
        "--file", str(packet),
        "--write-output", str(output),
    ]


def run_one(
    *,
    db_path: Path,
    recipient: str,
    worker_id: str,
    profile: Path,
    state_dir: Path,
    npx: Path,
    model: str = "gpt-5.6",
    oracle_timeout: str = "auto",
    process_timeout: int = 7200,
    provider_lock: Path | None = None,
    execute: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict:
    lock_path = provider_lock or Path(db_path).with_name("provider.lock")
    with BUS.provider_slot(lock_path) as acquired:
        if not acquired:
            return {"status": "busy"}
        return _run_one_locked(
            db_path=db_path, recipient=recipient, worker_id=worker_id,
            profile=profile, state_dir=state_dir, npx=npx, model=model,
            oracle_timeout=oracle_timeout, process_timeout=process_timeout,
            execute=execute,
        )


def _run_one_locked(
    *,
    db_path: Path,
    recipient: str,
    worker_id: str,
    profile: Path,
    state_dir: Path,
    npx: Path,
    model: str,
    oracle_timeout: str,
    process_timeout: int,
    execute: Callable[..., subprocess.CompletedProcess[str]],
) -> dict:
    task = BUS.claim(db_path, recipient=recipient, worker_id=worker_id)
    if task is None:
        return {"status": "idle"}
    task_id = int(task["task_id"])
    lease = task["lease_token"]
    run_root = Path(state_dir).resolve() / f"task-{task_id}"
    run_root.mkdir(parents=True, exist_ok=True)
    packet = run_root / "task.md"
    answer = run_root / "answer.md"
    inputs = BUS.task_inputs(
        db_path, task_id=task_id, recipient=recipient, lease_token=lease
    )
    sections = [
        "# Independent meeting task",
        f"Round: `{task['round_id']}`",
        f"Seat: `{task['to']}`",
    ]
    sections.extend(f"## ref:{item['ref']}\n\n{item['body']}" for item in inputs)
    packet.write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    argv = oracle_argv(
        npx=npx, profile=Path(profile).resolve(), packet=packet, output=answer,
        task_id=task_id, timeout=oracle_timeout, model=model, stage=task["stage"],
    )
    env = os.environ.copy()
    # Keep the caller-selected launcher directory. Resolving snap's npx symlink
    # yields /usr/bin/snap, which would put an older /usr/bin/node first.
    env["PATH"] = os.pathsep.join([str(Path(npx).parent), env.get("PATH", "")])
    try:
        completed = execute(
            argv,
            cwd=run_root,
            env=env,
            text=True,
            capture_output=True,
            timeout=int(process_timeout),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return BUS.attention(
            db_path, task_id=task_id, recipient=recipient, lease_token=lease,
            error=f"Oracle timed out; submission state is unknown: {exc}",
        )
    except OSError as exc:
        return BUS.attention(
            db_path, task_id=task_id, recipient=recipient, lease_token=lease,
            error=f"Oracle could not start: {exc}",
        )
    if completed.returncode != 0 or not answer.is_file() or not answer.read_text(encoding="utf-8").strip():
        detail = (completed.stderr or completed.stdout or "Oracle returned no answer")[-2000:]
        return BUS.attention(
            db_path, task_id=task_id, recipient=recipient, lease_token=lease,
            error=f"Oracle did not prove completion; no automatic retry. {detail}",
        )
    return BUS.complete(
        db_path,
        task_id=task_id,
        recipient=recipient,
        lease_token=lease,
        result=answer.read_text(encoding="utf-8"),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--recipient", default="chatgpt")
    parser.add_argument("--worker-id", default="chatgpt-browser")
    parser.add_argument("--npx", type=Path, default=DEFAULT_NPX)
    parser.add_argument("--model", default="gpt-5.6")
    parser.add_argument("--oracle-timeout", default="auto")
    parser.add_argument("--process-timeout", type=int, default=7200)
    parser.add_argument("--provider-lock", type=Path)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--interval", type=float, default=5.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    while True:
        payload = run_one(
            db_path=args.db,
            recipient=args.recipient,
            worker_id=args.worker_id,
            profile=args.profile,
            state_dir=args.state_dir,
            npx=args.npx,
            model=args.model,
            oracle_timeout=args.oracle_timeout,
            process_timeout=args.process_timeout,
            provider_lock=args.provider_lock,
        )
        print(payload, flush=True)
        if not args.serve:
            return 0 if payload.get("status") != "attention_required" else 2
        time.sleep(max(1.0, args.interval) if payload.get("status") == "idle" else 0.1)


if __name__ == "__main__":
    raise SystemExit(main())
