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
import sys
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
