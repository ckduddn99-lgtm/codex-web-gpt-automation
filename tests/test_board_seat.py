"""Seat-client tests that run without a Discord token.

Everything here is the part that fails silently in production: a chunker that
cuts a sentence in half, a cursor that lets a seat wake itself up, a room name
whose case does not survive Discord's lowercasing, an HTTP error that tells the
operator nothing. The network calls themselves are stubbed -- what is worth
asserting is the behaviour around them.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "board_seat.py"


def load_module():
    name = "board_seat_test"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


board = load_module()


# --------------------------------------------------------------------------
# chunking
# --------------------------------------------------------------------------

def test_short_message_is_one_chunk():
    assert board.chunk_message("hello") == ["hello"]


def test_every_chunk_stays_within_the_discord_limit():
    text = "\n".join(f"line {i} " + "x" * 90 for i in range(200))
    chunks = board.chunk_message(text)
    assert len(chunks) > 1
    assert all(len(c) <= board.MESSAGE_LIMIT for c in chunks)


def test_chunking_prefers_line_boundaries_over_hard_cuts():
    """A transcript people read on a phone should not break mid-sentence."""
    paragraph = "x" * 1500
    text = paragraph + "\n" + paragraph
    chunks = board.chunk_message(text)
    assert chunks == [paragraph, paragraph]


def test_a_single_overlong_line_is_cut_rather_than_dropped():
    text = "y" * (board.MESSAGE_LIMIT * 2 + 5)
    chunks = board.chunk_message(text)
    assert "".join(chunks) == text
    assert all(len(c) <= board.MESSAGE_LIMIT for c in chunks)


def test_chunking_loses_no_content():
    text = "\n".join(f"{i}: " + "z" * 200 for i in range(60))
    assert "\n".join(board.chunk_message(text)) == text


# --------------------------------------------------------------------------
# room names
# --------------------------------------------------------------------------

def test_room_name_gets_the_channel_prefix():
    assert board.room_channel_name("lobby") == "room-lobby"


def test_an_already_prefixed_room_name_is_not_doubled():
    assert board.room_channel_name("room-lobby") == "room-lobby"


def test_uppercase_room_names_are_rejected_up_front():
    """Discord lowercases channel names, so a mixed-case room silently misses."""
    with pytest.raises(board.BoardError):
        board.room_channel_name("Lobby")


def test_too_short_room_names_are_rejected():
    with pytest.raises(board.BoardError):
        board.room_channel_name("ab")


def test_korean_room_names_are_allowed():
    """Discord accepts non-ASCII channel names, and this board is used in Korean.

    The first cut rejected anything outside [a-z0-9-], which would have refused
    every natural room name the operator would actually type.
    """
    assert board.room_channel_name("회의방") == "room-회의방"


def test_room_names_with_characters_discord_rewrites_are_rejected():
    """A space becomes a dash in the real channel name, so the lookup would miss."""
    for bad in ("my room", "a#b", "a/b", "a.b"):
        with pytest.raises(board.BoardError):
            board.room_channel_name(bad)


# --------------------------------------------------------------------------
# token loading
# --------------------------------------------------------------------------

def test_token_is_read_from_the_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    p = tmp_path / ".board.env"
    p.write_text("# comment\nDISCORD_BOT_TOKEN=abc.def.ghi\n", encoding="utf-8")
    assert board.load_token(p) == "abc.def.ghi"


def test_a_missing_token_file_names_the_script_that_creates_it(tmp_path, monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    with pytest.raises(board.BoardError) as e:
        board.load_token(tmp_path / "nope.env")
    assert "set-board-token" in str(e.value)


def test_an_empty_token_is_rejected_rather_than_sent(tmp_path, monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    p = tmp_path / ".board.env"
    p.write_text("DISCORD_BOT_TOKEN=\n", encoding="utf-8")
    with pytest.raises(board.BoardError):
        board.load_token(p)


# --------------------------------------------------------------------------
# error messages
# --------------------------------------------------------------------------

def test_401_tells_the_operator_to_reset_the_token():
    msg = board._explain_http(401, b'{"message": "401: Unauthorized"}')
    assert "set-board-token" in msg


def test_403_points_at_the_channel_permissions():
    msg = board._explain_http(403, b'{"message": "Missing Access"}')
    assert "permission" in msg.lower()
    assert "Missing Access" in msg


# --------------------------------------------------------------------------
# message ordering and cursors
# --------------------------------------------------------------------------

class _FakeClient(board.Client):
    """Client with the network replaced; everything above the wire still runs."""

    def __init__(self, responses):
        super().__init__(token="x.y.z")
        self.responses = responses
        self.calls = []

    def _request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        value = self.responses.get((method, path.split("?")[0]))
        if callable(value):
            return value(path, payload)
        return value


def _msg(mid, content="hi"):
    return {"id": mid, "content": content, "timestamp": "2026-09-07T12:00:00.000000+00:00",
            "author": {"username": "seat1"}}


def test_messages_come_back_oldest_first():
    """Discord answers newest-first even with `after`; a transcript reads forward."""
    client = _FakeClient({("GET", "/channels/1/messages"): [_msg("3"), _msg("2"), _msg("1")]})
    got = client.messages_after("1", after=None)
    assert [m["id"] for m in got] == ["1", "2", "3"]


def test_the_after_cursor_is_sent_to_discord():
    client = _FakeClient({("GET", "/channels/1/messages"): []})
    client.messages_after("1", after="42")
    assert "after=42" in client.calls[0][1]


def test_a_long_post_is_split_into_several_requests():
    client = _FakeClient({("POST", "/channels/1/messages"): lambda path, body: {"id": "9"}})
    client.post("1", "q" * (board.MESSAGE_LIMIT + 10))
    assert len(client.calls) == 2
    assert all(len(c[2]["content"]) <= board.MESSAGE_LIMIT for c in client.calls)


def test_posting_advances_the_seats_own_cursor(tmp_path, monkeypatch):
    """A seat must not be woken by its own message, or wait() returns instantly."""
    monkeypatch.setattr(board, "STATE_DIR", tmp_path)
    board.save_state("lobby", "seat1", {"cursor": "1"})
    board.save_state("lobby", "seat1", {"cursor": "7"})
    assert board.load_state("lobby", "seat1")["cursor"] == "7"


def test_seat_state_survives_a_dead_session(tmp_path, monkeypatch):
    """The cursor is a Discord message id on disk, so a restarted seat resumes."""
    monkeypatch.setattr(board, "STATE_DIR", tmp_path)
    board.save_state("lobby", "seat2", {"cursor": "123", "status": "researching"})
    reloaded = board.load_state("lobby", "seat2")
    assert reloaded == {"cursor": "123", "status": "researching"}


def test_two_seats_in_one_room_keep_separate_cursors(tmp_path, monkeypatch):
    monkeypatch.setattr(board, "STATE_DIR", tmp_path)
    board.save_state("lobby", "seat1", {"cursor": "10"})
    board.save_state("lobby", "seat2", {"cursor": "20"})
    assert board.load_state("lobby", "seat1")["cursor"] == "10"
    assert board.load_state("lobby", "seat2")["cursor"] == "20"


# --------------------------------------------------------------------------
# guild / channel resolution
# --------------------------------------------------------------------------

def test_no_server_says_to_authorize_the_bot():
    client = _FakeClient({("GET", "/users/@me/guilds"): []})
    with pytest.raises(board.BoardError) as e:
        board.resolve_guild(client)
    assert "authorize" in str(e.value).lower()


def test_several_servers_ask_for_an_explicit_choice():
    client = _FakeClient({("GET", "/users/@me/guilds"): [
        {"id": "1", "name": "a"}, {"id": "2", "name": "b"}]})
    with pytest.raises(board.BoardError) as e:
        board.resolve_guild(client)
    assert "--guild" in str(e.value)


def test_voice_channels_and_categories_are_not_mistaken_for_rooms():
    client = _FakeClient({("GET", "/guilds/1/channels"): [
        {"id": "9", "name": "room-lobby", "type": 2},   # voice
        {"id": "8", "name": "room-lobby", "type": 4},   # category
        {"id": "7", "name": "room-lobby", "type": 0},   # text
    ]})
    assert board.resolve_channel(client, "1", "room-lobby")["id"] == "7"


def test_a_missing_channel_says_to_create_it():
    client = _FakeClient({("GET", "/guilds/1/channels"): []})
    with pytest.raises(board.BoardError) as e:
        board.resolve_channel(client, "1", "script")
    assert "Create it" in str(e.value)
