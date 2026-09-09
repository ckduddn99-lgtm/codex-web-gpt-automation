#!/usr/bin/env python3
"""Tell the user, in Discord, only about the things worth interrupting them for.

Discord is the person's window, not a transport. Prompts and answers stay on the bus;
what crosses into a channel is a state transition and a short reason, because a room
that reports every poll teaches the person to stop reading it.

Two properties this has to hold:

  Only meaningful events. A round waiting on seats is normal operation. Consensus
  reached, consensus denied, a stage blocked on somebody, and a proposal now waiting for
  approval are the four things a person can act on -- everything else is noise.

  Never the same thing twice. `advance` is called on a timer, so an unchanged blocked
  stage would otherwise announce itself on every tick. Events are keyed by what actually
  differs and suppressed for a cooldown, and the key deliberately includes who is
  blocking so a second seat failing is still news.

Writing goes through the seat bot, which Discord forbids from posting in the conductor
lane. That is why the conductor token is not on this host at all: a process that could
read it could write the instructions it is supposed to be following.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable


def _load_seat():
    spec = importlib.util.spec_from_file_location(
        "board_seat", Path(__file__).resolve().parent / "board_seat.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("board_seat", module)
    spec.loader.exec_module(module)
    return module


SEAT = _load_seat()

DEFAULT_CHANNEL = "일반"
DEFAULT_COOLDOWN_SECONDS = 3600.0
STATE_PATH = SEAT.REPO_ROOT / ".board-state" / "notify.json"


def _blocked_signature(payload: dict[str, Any]) -> str:
    rows = payload.get("blocked") or []
    return ",".join(sorted(f"{row.get('participant')}:{row.get('why')}" for row in rows))


def classify(payload: dict[str, Any]) -> tuple[str, str] | None:
    """Return (event key, message) when this transition is worth a person's attention."""
    round_id = payload.get("round_id", "?")
    action = payload.get("action")
    blocked = payload.get("blocked") or []

    if action in {"goal_transition", "goal_task_transition"}:
        goal_id = payload.get("goal_id", "?")
        task_id = payload.get("task_id")
        entity = f"{goal_id}/{task_id}" if task_id else str(goal_id)
        from_status = payload.get("from_status")
        to_status = payload.get("to_status")
        assigned = payload.get("assignee") or payload.get("owner") or "?"
        changed_by = payload.get("changed_by") or "?"
        return (
            f"backlog:{entity}:{from_status}->{to_status}:{assigned}",
            f"📌 **백로그 상태 변경** — `{entity}`\n"
            f"`{from_status or 'new'}` → `{to_status}` · 담당 `{assigned}` · 변경 `{changed_by}`",
        )
    if action == "goal_task_run_completed":
        goal_id = str(payload.get("goal_id", "?"))
        task_id = str(payload.get("task_id", "?"))
        run_id = payload.get("run_id") or "?"
        result_status = payload.get("result_status") or "?"
        return (
            f"goal-task-run:{goal_id}:{task_id}:{run_id}:{result_status}",
            f"⚙️ **목표 작업 상태 변경** — `{goal_id}/{task_id}` · run `{run_id}`\n"
            f"실행 결과 `{result_status}` · 원문 결과는 서버 원장에만 보관합니다.",
        )
    if action == "goal_task_run_attention":
        goal_id = str(payload.get("goal_id", "?"))
        task_id = str(payload.get("task_id", "?"))
        run_id = payload.get("run_id") or "?"
        code = payload.get("error_code") or "GOAL_TASK_RUN_ATTENTION"
        public_reason = str(payload.get("public_reason") or "").strip()
        reason_line = f"\n원인: {public_reason}" if public_reason else ""
        return (
            f"goal-task-attention:{goal_id}:{task_id}:{run_id}:{code}",
            f"⚠️ **목표 작업 막힘 감지** — `{goal_id}/{task_id}` · run `{run_id}`\n"
            f"`{code}` · 원 실행을 무작정 반복하지 않고 복구 루프가 상태를 진단합니다.{reason_line}",
        )
    if action == "goal_task_recovery_scheduled":
        goal_id = str(payload.get("goal_id", "?"))
        original = str(payload.get("original_task_id", "?"))
        recovery = str(payload.get("task_id", "?"))
        run_id = payload.get("original_run_id") or "?"
        attempt = payload.get("attempt") or "?"
        classification = payload.get("classification") or "unknown"
        reason = str(payload.get("reason") or "").strip()
        return (
            f"goal-task-recovery:{goal_id}:{run_id}:{attempt}:scheduled",
            f"🛠️ **자동 복구 시작** — `{goal_id}/{original}` · 원 run `{run_id}`\n"
            f"분류 `{classification}` · 복구 `{recovery}` · 시도 `{attempt}/6`"
            + (f"\n진단: {reason}" if reason else ""),
        )
    if action == "goal_task_recovery_resumed":
        goal_id = str(payload.get("goal_id", "?"))
        task_id = str(payload.get("task_id", "?"))
        run_id = payload.get("original_run_id") or "?"
        attempt = payload.get("attempt", "?")
        return (
            f"goal-task-recovery:{goal_id}:{run_id}:{attempt}:resumed",
            f"✅ **자동 복구 완료 · 원 작업 재개** — `{goal_id}/{task_id}`\n"
            f"원 run `{run_id}` · 복구 시도 `{attempt}` · 담당 `{payload.get('assignee') or 'chatgpt'}`",
        )
    if action == "goal_task_recovery_escalated":
        goal_id = str(payload.get("goal_id", "?"))
        task_id = str(payload.get("task_id", "?"))
        run_id = payload.get("original_run_id") or "?"
        reason = str(payload.get("reason") or "사용자 판단이 필요합니다.")
        return (
            f"goal-task-recovery:{goal_id}:{run_id}:escalated",
            f"🧑‍💻 **자동 복구 한계 · 사용자 확인 필요** — `{goal_id}/{task_id}`\n"
            f"원 run `{run_id}`\n{reason}",
        )
    if action in {"goal_driver_attention", "attention_required"} and payload.get("goal_id"):
        goal_id = str(payload["goal_id"])
        run_id = payload.get("run_id") or "?"
        code = payload.get("error_code") or payload.get("reason") or "GOAL_DRIVER_ATTENTION_REQUIRED"
        return (
            f"goal-driver:{goal_id}:{run_id}:{code}",
            f"⚠️ **목표 관리자 확인 필요** — `{goal_id}` / run `{run_id}`\n"
            f"`{code}` · 자동 재시도하지 않습니다.",
        )
    if action == "finalized":
        return (
            f"{round_id}:finalized",
            f"✅ **합의 도달** — `{round_id}`\n"
            f"전원이 명시적으로 승인했습니다. ({payload.get('decided_at', '')})",
        )
    if action == "no_consensus":
        return (
            f"{round_id}:no_consensus:{payload.get('code')}",
            f"🚫 **합의 실패** — `{round_id}`\n"
            f"`{payload.get('code')}` · {payload.get('detail', '')}\n"
            "반대·기권·미투표는 찬성으로 세지 않습니다. 제안을 고치려면 새 라운드가 필요합니다.",
        )
    if action == "publish_proposal":
        return (
            f"{round_id}:proposal",
            f"🗳️ **승인 대기** — `{round_id}`\n"
            "최종 제안이 게시됐습니다. 전원 명시적 승인이 있어야 통과합니다.",
        )
    if blocked:
        rows = "\n".join(
            f"· **{row.get('participant')}** — {row.get('why')}" for row in blocked
        )
        return (
            f"{round_id}:blocked:{payload.get('stage')}:{_blocked_signature(payload)}",
            f"⚠️ **진행 막힘** — `{round_id}` / `{payload.get('stage')}` 단계\n{rows}\n"
            "자동 재시도하지 않습니다. 이미 실행됐을 수 있어서입니다.",
        )
    return None


def _load_state(path: Path) -> dict[str, float]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A missing or unreadable ledger must not stop an alert; the worst case is one
        # repeat, and the worst case of the opposite is silence about a blocked round.
        return {}


def _save_state(path: Path, state: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def notify(
    payload: dict[str, Any], *, channel: str = DEFAULT_CHANNEL,
    cooldown: float = DEFAULT_COOLDOWN_SECONDS, state_path: Path = STATE_PATH,
    guild: str | None = None, dry_run: bool = False, now: float | None = None,
    post: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """`post` exists so the suppression rules can be exercised without a network.

    Dry run deliberately records nothing, because a rehearsal that consumed the
    cooldown would hide the next real alert.
    """
    classified = classify(payload)
    if classified is None:
        return {"sent": False, "reason": "not_notable", "action": payload.get("action")}
    key, message = classified

    state = _load_state(state_path)
    moment = time.time() if now is None else now
    last = state.get(key)
    if last is not None and moment - last < cooldown:
        return {"sent": False, "reason": "cooldown", "key": key,
                "seconds_left": round(cooldown - (moment - last), 1)}

    if dry_run:
        return {"sent": False, "reason": "dry_run", "key": key, "message": message}

    if post is None:
        def post(body: str) -> None:
            client = SEAT.Client(SEAT.load_token())
            guild_row = SEAT.resolve_guild(client, guild)
            channel_row = SEAT.resolve_channel(client, guild_row["id"], channel)
            client.post(channel_row["id"], body)

    post(message)

    state[key] = moment
    _save_state(state_path, state)
    return {"sent": True, "key": key, "channel": channel}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--channel", default=DEFAULT_CHANNEL,
                        help="channel to post into; never the conductor lane")
    parser.add_argument("--guild")
    parser.add_argument("--cooldown", type=float, default=DEFAULT_COOLDOWN_SECONDS)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--payload-file", type=Path,
                        help="driver output; defaults to stdin")
    return parser


def main(argv: list[str] | None = None, *, output: Callable[[str], None] = print) -> int:
    args = build_parser().parse_args(argv)
    raw = args.payload_file.read_text(encoding="utf-8") if args.payload_file else sys.stdin.read()
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        output(json.dumps({"sent": False, "reason": "unreadable_payload", "error": str(exc)}))
        return 2
    if args.channel == SEAT.SCRIPT_CHANNEL:
        # The instruction lane is the person's. A bot writing there would let a process
        # author the orders it is meant to be carrying out.
        output(json.dumps({"sent": False, "reason": "conductor_lane_refused"}))
        return 2
    result = notify(payload, channel=args.channel, cooldown=args.cooldown,
                    guild=args.guild, dry_run=args.dry_run)
    output(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
