#!/usr/bin/env python
"""`split N`: one operator command, N independent chat sessions, one meeting board.

The user-facing gesture is a single split. Underneath, N ordinary sessions are created
one at a time and each is handed a seat on the same board — the fan-out half of what
`chatgpt_meeting_board.py` collects.

This is a planner, not a second runner. Session creation, wave bounding, worktree
ownership, the distinct-session-locator check, and merging already live in
`chatgpt_oracle_multi.py`; this module writes the missions and the manifest that runner
already understands and then calls it. Reimplementing any of that would produce a second
system that drifts from the first.

Two things it does own:

* **Sequential by default.** `max_concurrency` is 1. Opening several sessions at once has
  failed before in ways that were not independent of each other, so seats are filled one
  at a time until 4/4 is boring.
* **A failed seat is withdrawn, not fatal.** If a session never comes up, its seat is
  dropped from the roster on the record and the remaining sessions still get to seal.
  Without this one broken browser turn freezes the whole room, which is exactly the
  failure that made the previous audit unusable.

What it cannot do: prove that the browser path works. `split` inherits every risk of the
model-picker and thinking-effort automation it launches through, and that path has not
been verified against a live session since it was last fixed. A one-seat split is the
cheapest way to find out.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Callable

BIN = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:  # pragma: no cover - packaging failure
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BOARD = _load("chatgpt_split_board", BIN / "chatgpt_meeting_board.py")
ORACLE_MULTI = _load("chatgpt_split_oracle_multi", BIN / "chatgpt_oracle_multi.py")

PLAN_SCHEMA = "codex.chatgpt.split-sessions-plan/v1"
REPORT_SCHEMA = "codex.chatgpt.split-sessions-report/v1"
MIN_SEATS = BOARD.MIN_PARTICIPANTS
MAX_SEATS = BOARD.MAX_PARTICIPANTS


class SplitError(RuntimeError):
    pass


def seat_ids(count: int) -> list[str]:
    return [f"seat{index}" for index in range(1, count + 1)]


def _merger_mission(runtime, *, question: str) -> str:
    script = str((BIN / "chatgpt_meeting_board.py").resolve())
    return f"""# Meeting board — synthesis for room `{runtime.room_id}`

Every seat has answered independently and none of them saw another's answer. Your job is
to seal the room, read what came back, and decide whether the disagreement is real.

1. `{script} seal --room-root {runtime.root}`
2. `{script} read-bundle --room-root {runtime.root} --participant <seat> --token-file <its token file>`

Then judge. Open an issue **only** where the answers actually conflict on something that
changes a decision — name the conflict and the seats it is between:

`{script} open-issue --room-root {runtime.root} --issue-id <id> --summary <what splits them> --participants <a,b>`

Convergence is a result, not a failure. If the seats agree, say so and open nothing: a
room with no conflict has nothing for cross-examination to do, and manufacturing one
would be inventing disagreement rather than finding it.

Do not count votes. Do not average the answers. Where they differ, the question is which
one the evidence supports.

## The original question

{question}
"""


def build_split_plan(
    *,
    project_root: Path,
    question: str,
    seats: int,
    output_dir: Path,
    room_id: str | None = None,
    app_name: str | None = None,
    model: str | None = None,
    model_strategy: str | None = None,
    max_concurrency: int = 1,
    private_root: Path | None = None,
) -> dict[str, Any]:
    """Create the room, write one mission per seat, and emit the runner's manifest."""
    if not isinstance(seats, int) or not MIN_SEATS <= seats <= MAX_SEATS:
        raise SplitError(f"split needs {MIN_SEATS}-{MAX_SEATS} seats")
    if not 1 <= int(max_concurrency) <= seats:
        raise SplitError("max_concurrency must be within 1..seats")
    project = Path(project_root).resolve(strict=True)
    output = Path(output_dir).resolve()
    missions_dir = output / "missions"
    missions_dir.mkdir(parents=True, exist_ok=True)

    participants = seat_ids(seats)
    runtime, tokens = BOARD.create_room(
        project,
        participants=participants,
        question=question,
        room_id=room_id,
        private_root=private_root,
    )

    solvers: list[dict[str, Any]] = []
    for participant in participants:
        # The token goes to host-only state and the mission gets its path. Mission bytes
        # are hashed into the run receipt, so the secret must not be among them.
        token_path = runtime.invites_path / f"{participant}.token"
        BOARD._write_private(token_path, (tokens[participant] + "\n").encode("ascii"))
        mission_path = missions_dir / f"{participant}.md"
        mission_path.write_text(
            BOARD.build_attach_prompt(
                runtime,
                participant=participant,
                token=tokens[participant],
                token_file=token_path,
                python_executable=sys.executable,
            ),
            encoding="utf-8",
        )
        solvers.append({"id": participant, "mission_path": str(mission_path), "access": "read-only"})

    merger_path = missions_dir / "synthesis.md"
    merger_path.write_text(_merger_mission(runtime, question=question), encoding="utf-8")

    manifest_payload = {
        "schema": ORACLE_MULTI.SCHEMA,
        "project_root": str(project),
        "output_dir": str(output),
        "app_name": app_name or "DevSpace",
        "model": model or "gpt-5.6",
        "model_strategy": model_strategy or "select",
        "max_concurrency": int(max_concurrency),
        "solvers": solvers,
        "merger_mission_path": str(merger_path),
    }
    manifest_path = output / "split-sessions.json"
    manifest_path.write_text(json.dumps(manifest_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    step = int(max_concurrency)
    return {
        "schema": PLAN_SCHEMA,
        "room_id": runtime.room_id,
        "room_root": str(runtime.root),
        "private_root": str(runtime.private_root),
        "manifest_path": str(manifest_path),
        "output_dir": str(output),
        "participants": participants,
        "max_concurrency": step,
        "waves": [
            {"index": index // step, "lane_ids": participants[index : index + step]}
            for index in range(0, len(participants), step)
        ],
    }


def _failed_lane_ids(result: dict[str, Any]) -> list[str]:
    failed = []
    for lane in result.get("lanes", []) or []:
        if not lane.get("ok"):
            lane_id = lane.get("id")
            if isinstance(lane_id, str):
                failed.append(lane_id)
    for lane_id in result.get("abandoned_lane_ids", []) or []:
        if isinstance(lane_id, str) and lane_id not in failed:
            failed.append(lane_id)
    return failed


def run_split(
    plan: dict[str, Any],
    *,
    dry_run: bool = False,
    execute: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Fill the seats, then withdraw the ones that never came up so the room can still seal."""
    kwargs: dict[str, Any] = {"dry_run": bool(dry_run)}
    if execute is not None:
        kwargs["execute"] = execute
    result = ORACLE_MULTI.run_multi(Path(plan["manifest_path"]), **kwargs)

    runtime = BOARD.open_room(Path(plan["room_root"]))
    withdrawn: list[dict[str, Any]] = []
    failed = _failed_lane_ids(result)
    requested = list(plan["participants"])

    if not dry_run and len(failed) >= len(requested):
        # Nothing came up. Withdrawing here would drop seats until the floor refused the
        # rest, leaving a room that reports the survivors as still pending when in fact
        # no session exists for them. A total failure is a fact about the launch, not a
        # roster change: leave the room untouched so a retry starts from a clean one.
        return {
            "schema": REPORT_SCHEMA,
            "status": "no_seat_came_up",
            "room_id": plan["room_id"],
            "room_root": plan["room_root"],
            "requested_seats": len(requested),
            "seated": [],
            "withdrawn": [],
            "failed_seats": failed,
            "independent_session_count": len(result.get("session_locators", []) or []),
            "submitted": result.get("submitted"),
            "dry_run": False,
            "runner": result,
        }

    if not dry_run:
        for lane_id in failed:
            try:
                dropped = BOARD.withdraw(
                    runtime,
                    participant=lane_id,
                    reason="the session for this seat never came up during split",
                )
            except BOARD.MeetingBoardError as exc:
                # A seat that already answered, or a floor violation. Report it rather
                # than force the roster: a room that cannot seal is a better outcome than
                # a bundle that quietly lost an answer somebody actually gave.
                withdrawn.append({"participant_id": lane_id, "withdraw_refused": exc.code})
                continue
            withdrawn.append(dropped["withdrawn"])

    status = BOARD.status(runtime)
    return {
        "schema": REPORT_SCHEMA,
        "status": "seated" if status["participants"] else "no_seat_came_up",
        "room_id": plan["room_id"],
        "room_root": plan["room_root"],
        "requested_seats": len(plan["participants"]),
        "seated": status["participants"],
        "withdrawn": withdrawn,
        "independent_session_count": len(result.get("session_locators", []) or []),
        "submitted": result.get("submitted"),
        "dry_run": bool(dry_run),
        "runner": result,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create N independent chat sessions and seat them on one meeting board."
    )
    parser.add_argument("seats", type=int, help=f"number of independent sessions ({MIN_SEATS}-{MAX_SEATS})")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--question-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--room-id")
    parser.add_argument("--app-name")
    parser.add_argument("--model")
    parser.add_argument("--model-strategy", choices=sorted(ORACLE_MULTI.MODEL_STRATEGIES))
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=1,
        help="seats opened at once; 1 by default because simultaneous opens have failed together",
    )
    parser.add_argument("--plan-only", action="store_true", help="write the room and missions, launch nothing")
    parser.add_argument("--dry-run", action="store_true", help="drive the runner to the submission boundary")
    return parser


def main(argv: list[str] | None = None, *, output: Callable[[str], None] = print) -> int:
    args = build_parser().parse_args(argv)
    try:
        plan = build_split_plan(
            project_root=args.project_root,
            question=args.question_file.read_text(encoding="utf-8"),
            seats=args.seats,
            output_dir=args.output_dir or (Path(args.project_root) / ".workflow" / "split-sessions"),
            room_id=args.room_id,
            app_name=args.app_name,
            model=args.model,
            model_strategy=args.model_strategy,
            max_concurrency=args.max_concurrency,
        )
    except (SplitError, BOARD.MeetingBoardError) as exc:
        code = getattr(exc, "code", "SPLIT_PLAN_INVALID")
        output(json.dumps({"ok": False, "code": code, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    if args.plan_only:
        output(json.dumps({"ok": True, **plan}, ensure_ascii=False, indent=2))
        return 0
    report = run_split(plan, dry_run=args.dry_run)
    output(json.dumps({"ok": True, **report}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    BOARD._force_utf8_stdio()
    raise SystemExit(main())
