#!/usr/bin/env python3
"""Seat-side client for a meeting board that lives in a Discord server.

Why there is no daemon here
---------------------------
An earlier sketch had a resident bridge holding a Gateway websocket, because a
seat that polls burns model tokens on every wake-up. That reasoning applies to
the *seat's model*, not to a process. ``wait`` blocks inside this CLI while it
polls the REST API a couple of times a second; the seat's session is parked in
a single tool call the whole time and spends nothing. So there is no websocket
to implement (this repository ships no third-party dependencies and a
hand-rolled RFC 6455 client would be real surface area), no daemon lifecycle,
and no in-memory room state that dies with the daemon.

Discord *is* the state. A seat's read cursor is a Discord message id, so a seat
whose session dies comes back to exactly where it was by calling ``wait`` again
-- the resumability requirement falls out of the design instead of being built.

What the board still enforces
-----------------------------
Seats arrive separately and argue; that is the whole point of the board, and it
only works if a participant cannot also issue orders. Instructions come from
``#script`` alone, which Discord's per-channel permissions make writable by the
conductor only. Everything this module returns from any other channel is data
that a seat reasons about -- never instructions it obeys.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Room names, seat names and the transcript itself are routinely Korean, and a
# Windows console still defaults to cp949 -- without this the board is unreadable
# exactly where it matters most.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

API = "https://discord.com/api/v10"
# Discord asks bots to identify themselves; a generic urllib agent gets 403s.
USER_AGENT = "DiscordBot (https://github.com/ckduddn99-lgtm/codex-web-gpt-automation, 0.1)"

MESSAGE_LIMIT = 2000
TEXT_CHANNEL = 0
# Discord's create-channel dialog offers these next to plain text, and picking
# the wrong one produces a channel with the right name that cannot hold a plain
# message. Naming the type back to the operator turns a mystifying "no such
# channel" into a one-line fix.
CHANNEL_TYPE_NAMES = {
    0: "text", 2: "voice", 4: "category", 5: "announcement",
    13: "stage", 15: "forum", 16: "media",
}
SCRIPT_CHANNEL = "script"
ROOM_PREFIX = "room-"

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".board.env"
# Role-specific on purpose. A shared DISCORD_BOT_TOKEN would let a conductor's
# exported token silently promote every seat command run in the same shell, and
# the whole point of running two bots is that a seat cannot hold the credential
# that writes instructions.
TOKEN_ENV_VAR = "BOARD_SEAT_TOKEN"
STATE_DIR = REPO_ROOT / ".board-state"


class BoardError(RuntimeError):
    """Something the operator has to fix, reported without a traceback."""


# --------------------------------------------------------------------------
# credentials
# --------------------------------------------------------------------------

def load_token(env_path: Path = ENV_PATH, env_var: str = TOKEN_ENV_VAR) -> str:
    """Read the bot token from a .env file, or from a role-specific variable.

    The file is the normal path because a token on a command line lands in the
    shell's history; scripts/set-board-token.ps1 writes it from a masked prompt.
    """
    env = os.environ.get(env_var)
    if env:
        return env.strip()
    if not env_path.exists():
        raise BoardError(
            f"{env_path} not found. Run: powershell -ExecutionPolicy Bypass "
            f"-File scripts\\set-board-token.ps1"
        )
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        if key.strip() == "DISCORD_BOT_TOKEN":
            token = value.strip()
            if not token:
                raise BoardError(f"DISCORD_BOT_TOKEN is empty in {env_path}")
            return token
    raise BoardError(f"No DISCORD_BOT_TOKEN line in {env_path}")


# --------------------------------------------------------------------------
# REST
# --------------------------------------------------------------------------

@dataclass
class Client:
    token: str
    max_retries: int = 5

    def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        url = f"{API}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "Authorization": f"Bot {self.token}",
            "User-Agent": USER_AGENT,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"

        for attempt in range(self.max_retries):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = resp.read()
                    return json.loads(body) if body else None
            except urllib.error.HTTPError as e:
                body = e.read()
                # 429 carries the exact wait in the body. Sleeping the amount
                # Discord names beats guessing a backoff curve, and getting it
                # wrong here gets the bot temporarily banned rather than slowed.
                if e.code == 429:
                    try:
                        retry_after = float(json.loads(body).get("retry_after", 1.0))
                    except Exception:
                        retry_after = 1.0
                    time.sleep(min(retry_after, 60.0) + 0.1)
                    continue
                if e.code in (500, 502, 503, 504) and attempt + 1 < self.max_retries:
                    time.sleep(2 ** attempt)
                    continue
                raise BoardError(_explain_http(e.code, body)) from None
            except urllib.error.URLError as e:
                if attempt + 1 < self.max_retries:
                    time.sleep(2 ** attempt)
                    continue
                raise BoardError(f"Could not reach Discord: {e.reason}") from None
        raise BoardError("Gave up after repeated rate limiting from Discord")

    def me(self) -> dict:
        return self._request("GET", "/users/@me")

    def guilds(self) -> list[dict]:
        return self._request("GET", "/users/@me/guilds")

    def channels(self, guild_id: str) -> list[dict]:
        return self._request("GET", f"/guilds/{guild_id}/channels")

    def messages_after(self, channel_id: str, after: str | None, limit: int = 100) -> list[dict]:
        query = f"?limit={limit}"
        if after:
            query += f"&after={after}"
        got = self._request("GET", f"/channels/{channel_id}/messages{query}")
        # Discord returns newest first regardless of `after`; a transcript reads
        # oldest first, and the cursor must end up on the newest of the batch.
        return list(reversed(got or []))

    def post(self, channel_id: str, content: str) -> list[dict]:
        posted = []
        for chunk in chunk_message(content):
            posted.append(self._request(
                "POST", f"/channels/{channel_id}/messages", {"content": chunk},
            ))
        return posted


def _explain_http(code: int, body: bytes) -> str:
    """Turn Discord's error codes into the thing the operator actually has to do."""
    try:
        detail = json.loads(body).get("message", "")
    except Exception:
        detail = body.decode("utf-8", "replace")[:200]
    if code == 401:
        return ("401 Unauthorized: the bot token is wrong or was reset. "
                "Re-run scripts\\set-board-token.ps1 with a fresh token.")
    if code == 403:
        return (f"403 Forbidden ({detail}). The bot is missing a permission in that "
                "channel -- check View Channels / Send Messages / Read Message History.")
    if code == 404:
        return f"404 Not Found ({detail}). The channel or guild id no longer exists."
    return f"Discord returned {code}: {detail}"


def chunk_message(content: str) -> list[str]:
    """Split to Discord's 2000-character limit, preferring line boundaries.

    A seat's answer is prose, and cutting mid-sentence makes the transcript hard
    to read for the humans watching on a phone. Splitting on newlines keeps
    paragraphs intact; only a single line longer than the limit gets cut hard.
    """
    content = content.rstrip("\n")
    if not content:
        return [""]
    if len(content) <= MESSAGE_LIMIT:
        return [content]

    chunks: list[str] = []
    current = ""
    for line in content.split("\n"):
        while len(line) > MESSAGE_LIMIT:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:MESSAGE_LIMIT])
            line = line[MESSAGE_LIMIT:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > MESSAGE_LIMIT:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


# --------------------------------------------------------------------------
# channel resolution
# --------------------------------------------------------------------------

def resolve_guild(client: Client, name: str | None = None) -> dict:
    guilds = client.guilds()
    if not guilds:
        raise BoardError(
            "The bot is not in any server yet. Open the OAuth2 URL Generator link "
            "and authorize it into your server."
        )
    if name:
        for g in guilds:
            if g["name"] == name:
                return g
        raise BoardError(f"No server named {name!r}; bot is in: "
                         + ", ".join(g["name"] for g in guilds))
    if len(guilds) > 1:
        raise BoardError("The bot is in several servers; pass --guild <name>: "
                         + ", ".join(g["name"] for g in guilds))
    return guilds[0]


def resolve_channel(client: Client, guild_id: str, name: str) -> dict:
    wrong_type = None
    for ch in client.channels(guild_id):
        if ch.get("name") != name:
            continue
        if ch.get("type") == TEXT_CHANNEL:
            return ch
        wrong_type = ch.get("type")
    if wrong_type is not None:
        kind = CHANNEL_TYPE_NAMES.get(wrong_type, f"type {wrong_type}")
        raise BoardError(
            f"#{name} exists but is a {kind} channel, which cannot hold plain "
            "messages. Delete it and create it again as a text channel."
        )
    raise BoardError(f"No text channel named #{name} in that server. Create it first.")


def room_channel_name(room: str) -> str:
    """Map a room name onto its channel name, rejecting names Discord would alter.

    Discord lowercases channel names and turns spaces into dashes, so a name that
    does not survive that trip resolves to a channel that does not exist -- and
    the failure reads as "no such channel" rather than "you typed it wrong".
    Korean and other non-cased scripts are legal channel names and pass through.
    """
    if len(room) < 3:
        raise BoardError(f"Room name {room!r} is too short (Discord requires at least 3 characters).")
    if room != room.lower():
        raise BoardError(
            f"Room name {room!r} has uppercase letters. Discord lowercases channel "
            "names, so this would silently miss; use " + room.lower() + "."
        )
    if re.search(r"[\s#@!,.:;/]", room) or "\\" in room:
        raise BoardError(
            f"Room name {room!r} contains a character Discord rewrites in channel "
            "names (whitespace or punctuation). Use letters, digits and dashes."
        )
    return room if room.startswith(ROOM_PREFIX) else ROOM_PREFIX + room


# --------------------------------------------------------------------------
# seat state
# --------------------------------------------------------------------------

def state_path(room: str, seat: str) -> Path:
    return STATE_DIR / f"{room}--{seat}.json"


def load_state(room: str, seat: str) -> dict:
    p = state_path(room, seat)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def save_state(room: str, seat: str, state: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    p = state_path(room, seat)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def render(messages: list[dict]) -> str:
    lines = []
    for m in messages:
        author = m.get("author", {}).get("username", "?")
        stamp = m.get("timestamp", "")[:19].replace("T", " ")
        lines.append(f"[{stamp}] {author}: {m.get('content', '')}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def client_for(args) -> "Client":
    """The client a command should use.

    board_conduct.py reuses these commands with the conductor's credential, and
    a command that reaches for the seat token itself would quietly run the
    conductor's reads as the seat bot. Letting the caller attach a client keeps
    which bot is acting an explicit property of the call.
    """
    attached = getattr(args, "board_client", None)
    return attached if attached is not None else Client(load_token())


def cmd_doctor(args) -> int:
    client = client_for(args)
    me = client.me()
    print(f"bot        : {me.get('username')}#{me.get('discriminator')} (id {me.get('id')})")
    guilds = client.guilds()
    if not guilds:
        print("servers    : none -- authorize the bot into a server first")
        return 1
    for g in guilds:
        print(f"server     : {g['name']} (id {g['id']})")
        all_channels = client.channels(g["id"])
        texts = [c for c in all_channels if c.get("type") == TEXT_CHANNEL]
        print("channels   : " + (", ".join("#" + c["name"] for c in texts) or "(none visible)"))
        names = {c["name"] for c in texts}
        # A channel with the right name but the wrong type is the confusing case:
        # the operator sees it in Discord and the bot reports it missing.
        for c in all_channels:
            if c.get("type") == TEXT_CHANNEL:
                continue
            n = c.get("name", "")
            if n == SCRIPT_CHANNEL or n.startswith(ROOM_PREFIX):
                kind = CHANNEL_TYPE_NAMES.get(c.get("type"), f"type {c.get('type')}")
                print(f"  !! #{n} is a {kind} channel, not a text channel -- "
                      "delete it and recreate it as text")
        if SCRIPT_CHANNEL not in names:
            print(f"  !! missing #{SCRIPT_CHANNEL} -- the conductor lane")
        if not any(n.startswith(ROOM_PREFIX) for n in names):
            print(f"  !! no #{ROOM_PREFIX}* channel -- create one, e.g. #{ROOM_PREFIX}lobby")
    return 0


def cmd_read(args) -> int:
    client = client_for(args)
    guild = resolve_guild(client, args.guild)
    channel = resolve_channel(client, guild["id"], room_channel_name(args.room))
    state = load_state(args.room, args.seat)
    after = None if args.all else state.get("cursor")
    messages = client.messages_after(channel["id"], after, limit=args.limit)
    if messages and not args.peek:
        state["cursor"] = messages[-1]["id"]
        save_state(args.room, args.seat, state)
    print(render(messages) if messages else "(nothing new)")
    return 0


def cmd_wait(args) -> int:
    """Block until something new appears, then return it.

    The seat's model is parked in this one tool call for the whole wait, so the
    interval below costs network calls, not tokens. Keep it short enough that a
    conversation does not feel laggy and long enough to stay far under Discord's
    per-route rate limit.
    """
    client = client_for(args)
    guild = resolve_guild(client, args.guild)
    channel = resolve_channel(client, guild["id"], room_channel_name(args.room))
    state = load_state(args.room, args.seat)
    deadline = time.monotonic() + args.timeout
    while True:
        messages = client.messages_after(channel["id"], state.get("cursor"), limit=100)
        if messages:
            state["cursor"] = messages[-1]["id"]
            save_state(args.room, args.seat, state)
            print(render(messages))
            return 0
        if time.monotonic() >= deadline:
            print("(timeout -- nothing new)")
            return 2
        time.sleep(args.interval)


def cmd_post(args) -> int:
    client = client_for(args)
    guild = resolve_guild(client, args.guild)
    channel = resolve_channel(client, guild["id"], room_channel_name(args.room))
    if args.file:
        content = Path(args.file).read_text(encoding="utf-8")
    else:
        content = args.text or sys.stdin.read()
    content = f"**{args.seat}** | {content}" if args.seat else content
    posted = client.post(channel["id"], content)
    # Posting advances this seat's own cursor: a seat should not be woken by its
    # own message, and without this every post returns immediately from wait().
    if posted:
        state = load_state(args.room, args.seat)
        state["cursor"] = posted[-1]["id"]
        save_state(args.room, args.seat, state)
    print(f"posted {len(posted)} message(s)")
    return 0


def cmd_script(args) -> int:
    """Read the conductor lane. This is the only channel carrying instructions."""
    client = client_for(args)
    guild = resolve_guild(client, args.guild)
    channel = resolve_channel(client, guild["id"], SCRIPT_CHANNEL)
    messages = client.messages_after(channel["id"], None, limit=args.limit)
    print(render(messages) if messages else "(no script posted yet)")
    return 0


def cmd_research(args) -> int:
    """Announce leaving to gather evidence, and coming back with it.

    Without this the room cannot tell a seat that is thinking from one that is
    gone, so it either advances past a seat that was about to answer or waits
    forever on one that died.
    """
    client = client_for(args)
    guild = resolve_guild(client, args.guild)
    channel = resolve_channel(client, guild["id"], room_channel_name(args.room))
    if args.action == "start":
        body = f"_{args.seat} is checking: {args.what}_"
    else:
        body = f"_{args.seat} is back_" + (f" -- {args.what}" if args.what else "")
    posted = client.post(channel["id"], body)
    if posted:
        state = load_state(args.room, args.seat)
        state["cursor"] = posted[-1]["id"]
        state["status"] = "researching" if args.action == "start" else "idle"
        save_state(args.room, args.seat, state)
    print(body)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="board_seat", description=__doc__.split("\n")[0])
    p.add_argument("--guild", help="server name, only needed if the bot is in several")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("doctor", help="check the token, server and channels")
    d.set_defaults(func=cmd_doctor)

    def room_args(sp, seat_required=True):
        sp.add_argument("--room", required=True, help="room name, e.g. lobby or room-lobby")
        sp.add_argument("--seat", required=seat_required, help="this seat's name")

    r = sub.add_parser("read", help="print messages, advancing this seat's cursor")
    room_args(r)
    r.add_argument("--limit", type=int, default=100)
    r.add_argument("--all", action="store_true", help="from the beginning, ignoring the cursor")
    r.add_argument("--peek", action="store_true", help="do not advance the cursor")
    r.set_defaults(func=cmd_read)

    w = sub.add_parser("wait", help="block until something new is said")
    room_args(w)
    w.add_argument("--timeout", type=float, default=600.0)
    w.add_argument("--interval", type=float, default=2.0)
    w.set_defaults(func=cmd_wait)

    o = sub.add_parser("post", help="say something in the room")
    room_args(o)
    o.add_argument("text", nargs="?", help="message text; omit to read stdin")
    o.add_argument("--file", help="post the contents of this file instead")
    o.set_defaults(func=cmd_post)

    s = sub.add_parser("script", help="read the conductor lane (#script)")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(func=cmd_script)

    e = sub.add_parser("research", help="announce leaving to gather evidence / returning")
    e.add_argument("action", choices=["start", "done"])
    room_args(e)
    e.add_argument("--what", default="", help="what is being checked, or what came back")
    e.set_defaults(func=cmd_research)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except BoardError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
