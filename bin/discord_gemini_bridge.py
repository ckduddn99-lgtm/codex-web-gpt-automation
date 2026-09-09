#!/usr/bin/env python3
"""Carry a person's Discord message to Gemini and bring the answer back.

This is not a meeting, so it does not go on the bus. A round is a question put to
several seats whose independence has to be enforced; a person asking Gemini something is
one request with one answer, and forcing it through round semantics would only make both
weaker. What it does share with the bus is the provider slot: the same advisory lock the
seat workers take, so a direct question cannot run a second heavyweight model beside a
meeting on a small host.

The lanes are the ones Discord already enforces. The person writes in the instruction
channel, where the seat bot is forbidden to post, and the answer comes back in the
general channel. Nothing here can write an instruction to itself, which is the same
reason the conductor token is not on this host.

Two things it refuses to do:

  Answer twice. The cursor advances only after the answer is delivered, and the model is
  never called again for a message that already failed -- the work may have happened.

  Answer silently. A person who asked a question and got nothing cannot tell a broken
  bridge from a slow one, so a failure says so in the channel and stops.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parent / filename
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


SEAT = _load("board_seat", "board_seat.py")
BUS = _load("chatgpt_server_bus", "chatgpt_server_bus.py")
AGY = _load("agy_server_worker", "agy_server_worker.py")
GOAL = _load("server_goal_driver", "server_goal_driver.py")
GOAL_TASK = _load("server_goal_task_worker", "server_goal_task_worker.py")
NOTIFY = _load("board_notify", "board_notify.py")

ANSWER_CHANNEL = "일반"
STATE_PATH = SEAT.REPO_ROOT / ".board-state" / "gemini-bridge.json"
GOAL_REPO_ROUTES = {
    "stock-ai-app": Path("/home/ckduddn99/stock-ai-app"),
}

SYSTEM_PREAMBLE = (
    "당신은 이 서버에 상주하는 Gemini입니다. 아래는 사용자가 Discord 지시 채널에 쓴 "
    "메시지입니다. 사용자에게 직접 답하세요.\n\n"
    "- 결론 먼저. 개조식이 문단을 대신하지 않게.\n"
    "- 확인하지 않은 것을 확인한 것처럼 쓰지 마세요. 모르면 무엇을 확인하면 되는지 지목하세요.\n"
    "- 이 요청은 회의가 아닙니다. 다른 좌석을 대신해 답하지 마세요.\n"
)


def _load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def human_messages(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only what a person wrote.

    The bots read this channel too, and an answer that quoted a bot back at Gemini would
    let the system talk to itself through the one lane meant to carry human intent.
    """
    return [
        row for row in rows
        if not (row.get("author") or {}).get("bot")
        and (row.get("content") or "").strip()
    ]


def ask_gemini(
    prompt: str, *, agy: Path, print_timeout: str = "5m", process_timeout: int = 420,
    execute: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[bool, str]:
    """Run one non-interactive Antigravity turn. Returns (ok, text)."""
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join([str(Path(agy).parent), env.get("PATH", "")])
    try:
        completed = execute(
            AGY.agy_argv(agy=agy, print_timeout=print_timeout),
            cwd=Path("/tmp") if os.name != "nt" else None,
            env=env, input=prompt, text=True, capture_output=True,
            timeout=int(process_timeout), check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"모델이 {process_timeout}초 안에 답하지 않았습니다."
    except FileNotFoundError:
        return False, f"{agy} 를 찾을 수 없습니다."
    answer = (completed.stdout or "").strip()
    if completed.returncode != 0 or not answer:
        detail = (completed.stderr or completed.stdout or "").strip()[-400:]
        return False, detail or "모델이 빈 응답을 돌려줬습니다."
    return True, answer


def _goal_retry_request(message: dict[str, Any]) -> tuple[int, str] | None:
    content = str(message.get("content") or "").strip()
    parts = content.split()
    if len(parts) != 4 or parts[:2] != ["/goal", "retry"]:
        return None
    try:
        run_id = int(parts[2])
    except ValueError:
        return None
    assignee = parts[3].strip().casefold()
    if assignee not in GOAL_TASK.ASSIGNEES:
        return None
    return run_id, assignee


def _goal_request(message: dict[str, Any]) -> tuple[str, str] | None:
    content = str(message.get("content") or "").strip()
    if _goal_retry_request(message) is not None:
        return None
    if content == "/goal":
        return "", ""
    if not content.startswith("/goal "):
        return None
    return f"discord-{message['id']}", content[6:].strip()


def handle_goal_retry_message(*, db_path: Path, message: dict[str, Any]) -> dict[str, Any]:
    parsed = _goal_retry_request(message)
    if parsed is None:
        raise ValueError("message is not a /goal retry request")
    run_id, assignee = parsed
    result = BUS.acknowledge_goal_task_run(
        db_path, run_id=run_id, changed_by="user",
        note=f"User explicitly reassigned run {run_id} after review.",
        requeue=True, reassign_to=assignee,
    )
    return {**result, "action": "goal_task_requeued", "assignee": assignee}


def handle_goal_message(
    *, db_path: Path, message: dict[str, Any], agy: Path,
    provider_lock: Path | None = None,
) -> dict[str, Any]:
    parsed = _goal_request(message)
    if parsed is None:
        raise ValueError("message is not a /goal request")
    goal_id, description = parsed
    if not description:
        return {"action": "goal_rejected", "reason": "goal_text_required"}
    try:
        created = BUS.create_goal(
            db_path, goal_id=goal_id, owner="gemini", created_by="user",
            description=description,
        )
    except BUS.BusError as exc:
        if exc.code != "GOAL_EXISTS":
            raise
        return {"action": "goal_exists", "goal_id": goal_id, "automatic_retry": False}
    return {
        "action": "goal_created", "goal_id": goal_id,
        "transition_id": created["transition_id"], "automatic_retry": False,
    }


def poll_once(
    *, agy: Path, guild: str | None = None, answer_channel: str = ANSWER_CHANNEL,
    state_path: Path = STATE_PATH, provider_lock: Path | None = None,
    db_path: Path | None = None,
    client: Any | None = None, ask: Callable[..., tuple[bool, str]] | None = None,
    goal_handler: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    retry_handler: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    max_messages: int = 5,
) -> dict[str, Any]:
    client = client or SEAT.Client(SEAT.load_token())
    ask = ask or (lambda prompt: ask_gemini(prompt, agy=agy))

    guild_row = SEAT.resolve_guild(client, guild)
    script_row = SEAT.resolve_channel(client, guild_row["id"], SEAT.SCRIPT_CHANNEL)
    answer_row = SEAT.resolve_channel(client, guild_row["id"], answer_channel)

    state = _load_state(state_path)
    pending = human_messages(client.messages_after(script_row["id"], state.get("cursor")))
    if not pending:
        return {"answered": 0, "reason": "nothing_new"}

    lock_path = provider_lock or Path(state_path).with_name("provider.lock")
    goal_db = db_path or Path.home() / ".local/state/ai-bus/bus.sqlite3"
    goal_handler = goal_handler or (
        lambda message: handle_goal_message(
            db_path=goal_db, message=message, agy=agy, provider_lock=lock_path
        )
    )
    retry_handler = retry_handler or (
        lambda message: handle_goal_retry_message(db_path=goal_db, message=message)
    )
    answered = 0
    for message in pending[:max_messages]:
        if _goal_retry_request(message) is not None:
            result = retry_handler(message)
            client.post(
                answer_row["id"],
                f"🔁 목표 작업 재배정: run `{result.get('run_id')}` → `{result.get('assignee')}`",
            )
            state = {**state, "cursor": message["id"]}
            _save_state(state_path, state)
            answered += 1
            continue
        if _goal_request(message) is not None:
            result = goal_handler(message)
            if result.get("action") == "goal_rejected":
                client.post(answer_row["id"], "⚠️ `/goal` 뒤에 목표 내용을 적어주세요.")
            else:
                goal_id = result.get("goal_id", "unknown")
                client.post(answer_row["id"], f"🎯 목표 등록: `{goal_id}`")
            state = {**state, "cursor": message["id"]}
            _save_state(state_path, state)
            answered += 1
            continue

        with BUS.provider_slot(lock_path) as acquired:
            if not acquired:
                return {
                    "answered": answered, "reason": "provider_busy",
                    "waiting": len(pending) - answered,
                }
            ok, text = ask(f"{SYSTEM_PREAMBLE}\n---\n{message.get('content', '')}")
        if not ok:
            client.post(answer_row["id"], f"⚠️ 답변 실패 — {text[:500]}")
            _save_state(state_path, {**state, "cursor": message["id"],
                                     "last_error": text[:500]})
            return {"answered": answered, "reason": "model_failed", "detail": text[:200]}
        client.post(answer_row["id"], text)
        state = {**state, "cursor": message["id"]}
        _save_state(state_path, state)
        answered += 1

    return {"answered": answered, "reason": "ok",
            "skipped": max(0, len(pending) - max_messages)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agy", type=Path, default=Path.home() / ".local/bin/agy")
    parser.add_argument("--guild")
    parser.add_argument("--answer-channel", default=ANSWER_CHANNEL)
    parser.add_argument("--provider-lock", type=Path)
    parser.add_argument("--db", type=Path, default=Path.home() / ".local/state/ai-bus/bus.sqlite3")
    parser.add_argument("--max-messages", type=int, default=5)
    return parser


def main(argv: list[str] | None = None, *, output: Callable[[str], None] = print) -> int:
    args = build_parser().parse_args(argv)
    if args.answer_channel == SEAT.SCRIPT_CHANNEL:
        output(json.dumps({"answered": 0, "reason": "conductor_lane_refused"}))
        return 2
    try:
        result = poll_once(
            agy=args.agy, guild=args.guild, answer_channel=args.answer_channel,
            provider_lock=args.provider_lock, db_path=args.db, max_messages=args.max_messages,
        )
        task_result = GOAL_TASK.run_one(
            db_path=args.db, repo=SEAT.REPO_ROOT, repo_routes=GOAL_REPO_ROUTES,
            agy=args.agy, provider_lock=args.provider_lock,
        )
        manager_result = GOAL.advance_all(
            db_path=args.db, agy=args.agy, provider_lock=args.provider_lock,
        )
        for payload in (task_result, manager_result):
            NOTIFY.notify(payload, channel=args.answer_channel, guild=args.guild)
    except (SEAT.BoardError, BUS.BusError, GOAL.GoalDriverError) as exc:
        output(json.dumps({"answered": 0, "reason": "bridge_error", "error": str(exc)},
                          ensure_ascii=False))
        return 2
    output(json.dumps({"bridge": result, "goal_task": task_result, "goal_manager": manager_result},
                      ensure_ascii=False))
    attention = {"model_failed", "bridge_error"}
    return 2 if result.get("reason") in attention or task_result.get("action") == "goal_task_run_attention" else 0


if __name__ == "__main__":
    raise SystemExit(main())
