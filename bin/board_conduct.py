#!/usr/bin/env python3
"""Conductor-side client: the only thing that can write instructions to #script.

Why this is a separate program with a separate token
-----------------------------------------------------
The board's value comes from seats that arrive separately and argue. That only
holds if a participant cannot also issue orders -- an arguing seat that can post
instructions is not a participant, it is the conductor with extra steps.

Discord enforces the split by identity, not by convention: #script denies Send
Messages to @everyone and grants it to the conductor bot's role alone. Verified
live on 2026-09-07 -- the seat bot gets 403 Missing Permissions on #script while
reading it fine, and the conductor bot posts.

That enforcement is only as good as the credential separation, so this program
reads .board-conductor.env and BOARD_CONDUCTOR_TOKEN, and board_seat.py reads
.board.env and BOARD_SEAT_TOKEN. Neither can pick up the other's token by
accident, which a shared DISCORD_BOT_TOKEN variable would have allowed.

Everything else -- the REST client, chunking, channel resolution -- is reused
from board_seat.py rather than copied, because two copies of a wire protocol
drift and the drift shows up as a room that half works.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

# bin/ is this script's own directory, so Python already has it on sys.path.
import board_seat
from board_seat import BoardError

CONDUCTOR_ENV_PATH = board_seat.REPO_ROOT / ".board-conductor.env"
CONDUCTOR_TOKEN_ENV_VAR = "BOARD_CONDUCTOR_TOKEN"


# A name no environment will define, so the comparison below reads the seat's
# file rather than whatever variable happens to be exported.
_NO_ENV_FALLBACK = "BOARD_TOKEN_ENV_VAR_THAT_IS_NEVER_SET"


def conductor_client() -> board_seat.Client:
    token = board_seat.load_token(CONDUCTOR_ENV_PATH, CONDUCTOR_TOKEN_ENV_VAR)
    seat_path = board_seat.ENV_PATH
    # The two files are one flag apart on the command line that writes them, so
    # check rather than trust. One token in both files silently collapses the
    # separation the whole design rests on, and the symptom would be a 403 that
    # reads like a Discord misconfiguration.
    if seat_path.exists() and token == board_seat.load_token(seat_path, _NO_ENV_FALLBACK):
        raise BoardError(
            f"{CONDUCTOR_ENV_PATH.name} holds the same token as {seat_path.name}. "
            "The seat and the conductor must be different bots, or #script's write "
            "restriction means nothing. Re-run scripts\\set-board-token.ps1 "
            "-Conductor with the conductor bot's token."
        )
    return board_seat.Client(token)


def _channel(client: board_seat.Client, guild_name: str | None, name: str) -> dict:
    guild = board_seat.resolve_guild(client, guild_name)
    return board_seat.resolve_channel(client, guild["id"], name)


def _body(args) -> str:
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    return args.text if args.text else sys.stdin.read()


def cmd_whoami(args) -> int:
    client = args.board_client
    me = client.me()
    print(f"conductor  : {me.get('username')} (id {me.get('id')})")
    guild = board_seat.resolve_guild(client, args.guild)
    print(f"server     : {guild['name']}")
    script = board_seat.resolve_channel(client, guild["id"], board_seat.SCRIPT_CHANNEL)
    print(f"script lane: #{script['name']} (id {script['id']})")
    return 0


def cmd_script(args) -> int:
    """Post instructions into the conductor lane."""
    client = args.board_client
    channel = _channel(client, args.guild, board_seat.SCRIPT_CHANNEL)
    posted = client.post(channel["id"], _body(args))
    print(f"posted {len(posted)} message(s) to #{board_seat.SCRIPT_CHANNEL}")
    return 0


def cmd_say(args) -> int:
    """Speak in a room as the conductor, without it counting as an instruction.

    Rooms are ordinary channels every seat can write to, so this is moderation
    and timekeeping -- 'we are on round two', 'seat3 has not answered' -- not a
    second instruction channel.
    """
    client = args.board_client
    channel = _channel(client, args.guild, board_seat.room_channel_name(args.room))
    posted = client.post(channel["id"], f"**지휘** | {_body(args)}")
    print(f"posted {len(posted)} message(s)")
    return 0


def cmd_watch(args) -> int:
    """Block until a room says something new, so the conductor can follow along.

    Same reasoning as the seat's wait: the poll happens in this process, so the
    conductor's model spends nothing while the room is quiet.
    """
    return board_seat.cmd_wait(args)


def cmd_read(args) -> int:
    return board_seat.cmd_read(args)


# A seat's join line and its speaking line, as board_seat.py writes them. The
# roster is read back out of the room rather than off local disk on purpose:
# seats run in their own sessions on their own machines, and the room is the
# only state all of them actually share.
JOIN_RE = re.compile(r"^_(?P<seat>[^\s]+) 착석 \((?P<family>[^,]+), (?P<verify>[^)]+)\)_")
SPEAK_RE = re.compile(r"^\*\*(?P<seat>[^*]+)\*\*")


def _roster(messages: list[dict]) -> dict[str, dict]:
    """Every seat that has announced itself in this room, in join order."""
    seats: dict[str, dict] = {}
    for m in messages:
        hit = JOIN_RE.match(m.get("content", "").strip())
        if hit:
            seats[hit.group("seat")] = {
                "family": hit.group("family").strip(),
                "verify": hit.group("verify").strip().startswith("확인 가능"),
            }
    return seats


def _round_start_index(messages: list[dict], conductor_id: str) -> int:
    """The conductor's most recent message is where this round began.

    Deriving it beats passing a timestamp in: the round starts when the question
    is asked, and that is exactly the message this finds.
    """
    for i in range(len(messages) - 1, -1, -1):
        if str(messages[i].get("author", {}).get("id")) == conductor_id:
            return i
    return 0


def _spoke_since(messages: list[dict], start: int) -> set[str]:
    said = set()
    for m in messages[start + 1:]:
        hit = SPEAK_RE.match(m.get("content", "").strip())
        if hit:
            said.add(hit.group("seat").strip())
    return said


def _composition(roster: dict[str, dict]) -> str:
    """The line that keeps "3/3" from being read as three independent samples."""
    families: dict[str, int] = {}
    for info in roster.values():
        families[info["family"]] = families.get(info["family"], 0) + 1
    verifying = sum(1 for i in roster.values() if i["verify"])
    family_part = ", ".join(f"{k} {v}" for k, v in sorted(families.items()))
    return (f"좌석 {len(roster)} · 계열 {len(families)}({family_part}) · "
            f"확인 가능 {verifying} / 불가 {len(roster) - verifying}")


def cmd_roll(args) -> int:
    """Report who has answered this round, and record who has not.

    A room that blocks on its slowest seat stops being a room. Some seats cannot
    stay resident at all -- a web session, a plan that ran out of quota, an agent
    attached to someone's conversation -- so the board has to tolerate that
    rather than require residency. A seat that misses the deadline is recorded
    as having missed it, which is itself information, and the round moves on.
    Anything it wants to say afterwards still lands as cross-examination.
    """
    client = args.board_client
    me = client.me()
    guild = board_seat.resolve_guild(client, args.guild)
    channel = board_seat.resolve_channel(
        client, guild["id"], board_seat.room_channel_name(args.room))

    deadline = time.monotonic() + args.deadline
    while True:
        messages = client.messages_after(channel["id"], None, limit=100)
        roster = _roster(messages)
        if not roster:
            raise BoardError(
                f"#{board_seat.room_channel_name(args.room)}에 착석한 좌석이 없습니다. "
                "좌석이 join을 돌렸는지 확인하세요."
            )
        start = _round_start_index(messages, str(me.get("id")))
        spoke = _spoke_since(messages, start)
        answered = [s for s in roster if s in spoke]
        missing = [s for s in roster if s not in spoke]
        if not missing or time.monotonic() >= deadline:
            break
        time.sleep(args.interval)

    def _label(seat: str) -> str:
        info = roster[seat]
        return f"{seat}({info['family']}, {'확인 가능' if info['verify'] else '확인 불가'})"

    lines = ["**점호**", "", _composition(roster), ""]
    lines.append("응답: " + (", ".join(_label(s) for s in answered) or "없음"))
    if missing:
        lines.append(f"**미응답**: " + ", ".join(_label(s) for s in missing))
        lines.append("")
        lines.append(f"마감 {int(args.deadline)}초를 넘겼다. 라운드는 진행한다 - 늦은 좌석의 "
                     "발언은 반대신문으로 들어온다. 미응답은 사실로 기록되며, 합의를 셀 때 "
                     "그 좌석은 표본에서 빠진다.")
    body = "\n".join(lines)
    client.post(channel["id"], body)
    print(body)
    return 0 if not missing else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="board_conduct", description=__doc__.split("\n")[0])
    p.add_argument("--guild", help="server name, only needed if the bot is in several")
    sub = p.add_subparsers(dest="command", required=True)

    w = sub.add_parser("whoami", help="check the conductor token and the script lane")
    w.set_defaults(func=cmd_whoami)

    s = sub.add_parser("script", help="post instructions to #script")
    s.add_argument("text", nargs="?", help="text; omit to read stdin")
    s.add_argument("--file", help="post the contents of this file instead")
    s.set_defaults(func=cmd_script)

    y = sub.add_parser("say", help="speak in a room as the conductor")
    y.add_argument("--room", required=True)
    y.add_argument("text", nargs="?")
    y.add_argument("--file")
    y.set_defaults(func=cmd_say)

    t = sub.add_parser("watch", help="block until a room says something new")
    t.add_argument("--room", required=True)
    t.add_argument("--seat", default="conductor", help="cursor name, defaults to conductor")
    t.add_argument("--timeout", type=float, default=600.0)
    t.add_argument("--interval", type=float, default=2.0)
    t.set_defaults(func=cmd_watch)

    l = sub.add_parser("roll", help="who answered this round, who did not")
    l.add_argument("--room", required=True)
    l.add_argument("--deadline", type=float, default=600.0,
                   help="seconds to wait before recording non-responders")
    l.add_argument("--interval", type=float, default=10.0)
    l.set_defaults(func=cmd_roll)

    r = sub.add_parser("read", help="print a room's transcript")
    r.add_argument("--room", required=True)
    r.add_argument("--seat", default="conductor")
    r.add_argument("--limit", type=int, default=100)
    r.add_argument("--all", action="store_true")
    r.add_argument("--peek", action="store_true")
    r.set_defaults(func=cmd_read)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        # Attach the credential once. The commands reused from board_seat pick
        # this up through client_for(), so the conductor's reads run as the
        # conductor rather than silently falling back to the seat bot.
        args.board_client = conductor_client()
        return args.func(args)
    except BoardError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
