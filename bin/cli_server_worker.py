#!/usr/bin/env python3
"""Claim Codex- or Claude-addressed tasks and answer through their official CLI."""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Sequence

import chatgpt_server_bus as BUS


# The framing that says whether the artifact is a question or a procedural step comes
# from the bus, because the worker is trusted code and the artifact is not. These are the
# constraints particular to a sandboxed CLI seat.
TOOL_RULE = (
    "Do not modify files, call external services, or infer another participant's draft. "
    "Return only your answer."
)


def provider_argv(*, provider: str, cli: Path) -> list[str]:
    if provider == "codex":
        return [
            str(cli), "exec", "--sandbox", "read-only", "--skip-git-repo-check",
            "--ephemeral", "--ignore-user-config", "--ignore-rules",
            "--color", "never", "-",
        ]
    if provider == "claude":
        return [
            str(cli), "-p", "--permission-mode", "plan", "--permission-prompts", "none",
            "--tools", "", "--safe-mode", "--strict-mcp-config",
            "--no-session-persistence", "--output-format", "text",
        ]
    raise ValueError(f"unsupported provider: {provider}")


def run_one(
    *,
    db_path: Path,
    recipient: str,
    worker_id: str,
    provider: str,
    cli: Path,
    process_timeout: int = 1800,
    provider_lock: Path | None = None,
    execute: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict:
    lock_path = provider_lock or Path(db_path).with_name("provider.lock")
    with BUS.provider_slot(lock_path) as acquired:
        if not acquired:
            return {"status": "busy"}
        return _run_one_locked(
            db_path=db_path, recipient=recipient, worker_id=worker_id,
            provider=provider, cli=cli, process_timeout=process_timeout,
            execute=execute,
        )


def _run_one_locked(
    *,
    db_path: Path,
    recipient: str,
    worker_id: str,
    provider: str,
    cli: Path,
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
    env["PATH"] = os.pathsep.join([str(Path(cli).parent), env.get("PATH", "")])
    try:
        with tempfile.TemporaryDirectory(prefix=f"ai-bus-{provider}-") as workdir:
            completed = execute(
                provider_argv(provider=provider, cli=cli),
                cwd=Path(workdir), env=env, input=packet, text=True,
                capture_output=True, timeout=int(process_timeout), check=False,
            )
    except subprocess.TimeoutExpired as exc:
        return BUS.attention(
            db_path, task_id=task_id, recipient=recipient, lease_token=lease,
            error=f"{provider} timed out; submission state is unknown: {exc}",
        )
    except OSError as exc:
        return BUS.attention(
            db_path, task_id=task_id, recipient=recipient, lease_token=lease,
            error=f"{provider} could not start: {exc}",
        )
    answer = (completed.stdout or "").strip()
    if completed.returncode != 0 or not answer:
        detail = (completed.stderr or completed.stdout or f"{provider} returned no answer")[-2000:]
        return BUS.attention(
            db_path, task_id=task_id, recipient=recipient, lease_token=lease,
            error=f"{provider} did not prove completion; no automatic retry. {detail}",
        )
    return BUS.complete(
        db_path, task_id=task_id, recipient=recipient, lease_token=lease,
        result=answer + "\n",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--provider", choices=("codex", "claude"), required=True)
    parser.add_argument("--recipient", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--process-timeout", type=int, default=1800)
    parser.add_argument("--provider-lock", type=Path)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--interval", type=float, default=5.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    while True:
        try:
            payload = run_one(
                db_path=args.db, recipient=args.recipient, worker_id=args.worker_id,
                provider=args.provider, cli=args.cli,
                process_timeout=args.process_timeout, provider_lock=args.provider_lock,
            )
        except BUS.BusError as exc:
            payload = {"status": "attention_required", "code": exc.code, "error": str(exc)}
        print(payload, flush=True)
        if not args.serve:
            return 0 if payload.get("status") != "attention_required" else 2
        time.sleep(max(1.0, args.interval) if payload.get("status") in {"idle", "busy"} else 0.1)


if __name__ == "__main__":
    raise SystemExit(main())
