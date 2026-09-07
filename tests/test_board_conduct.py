"""Conductor tests: the roll-call, and the credential split it depends on.

The roll-call exists because a room that blocks on its slowest seat stops being
a room, and some seats cannot stay resident at all. So the thing worth asserting
is that a missing seat is *recorded* rather than waited on forever, and that the
tally it prints cannot be misread as more independent samples than it is.

The roster is parsed back out of the room rather than read off local disk, so
these tests feed in message dicts shaped exactly like Discord's.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


BIN = Path(__file__).resolve().parents[1] / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

import board_conduct as conduct  # noqa: E402
import board_seat  # noqa: E402


CONDUCTOR_ID = "999"


def _msg(content, author_id="111"):
    return {"content": content, "author": {"id": author_id, "username": "회의"},
            "timestamp": "2026-09-07T12:00:00.000000+00:00"}


def _join(seat, family, verify=True):
    mark = "확인 가능" if verify else "확인 불가"
    return _msg(f"_{seat} 착석 ({family}, {mark})_")


def _speak(seat, text="발언"):
    return _msg(f"**{seat}** | {text}")


def _conduct(text="질문"):
    return _msg(f"**지휘** | {text}", author_id=CONDUCTOR_ID)


# --------------------------------------------------------------------------
# roster
# --------------------------------------------------------------------------

def test_the_roster_is_read_out_of_the_room():
    """Seats run in their own sessions; the room is the only state they share."""
    roster = conduct._roster([
        _join("gemini", "Google"),
        _speak("gemini"),
        _join("webgpt", "OpenAI", verify=False),
    ])
    assert set(roster) == {"gemini", "webgpt"}
    assert roster["gemini"] == {"family": "Google", "verify": True}
    assert roster["webgpt"] == {"family": "OpenAI", "verify": False}


def test_a_seat_that_rejoins_is_counted_once():
    roster = conduct._roster([_join("gemini", "Google"), _join("gemini", "Google")])
    assert list(roster) == ["gemini"]


def test_ordinary_messages_are_not_mistaken_for_joins():
    roster = conduct._roster([
        _speak("gemini", "_착석_ 이라는 단어가 들어간 평범한 발언"),
        _msg("_gemini 확인하러 감: 무언가_"),
    ])
    assert roster == {}


# --------------------------------------------------------------------------
# round boundary
# --------------------------------------------------------------------------

def test_the_round_starts_at_the_conductors_last_message():
    """The round begins when the question is asked, which is that message."""
    messages = [_join("a", "X"), _speak("a"), _conduct(), _speak("a")]
    assert conduct._round_start_index(messages, CONDUCTOR_ID) == 2


def test_with_no_conductor_message_the_whole_room_counts():
    messages = [_join("a", "X"), _speak("a")]
    assert conduct._round_start_index(messages, CONDUCTOR_ID) == 0


def test_only_speech_after_the_round_started_counts():
    """A seat that answered the *previous* question has not answered this one."""
    messages = [_join("a", "X"), _join("b", "Y"), _speak("a"), _conduct(), _speak("b")]
    start = conduct._round_start_index(messages, CONDUCTOR_ID)
    assert conduct._spoke_since(messages, start) == {"b"}


def test_research_notes_do_not_count_as_answering():
    """Announcing that you left to check something is not a turn."""
    messages = [_join("a", "X"), _conduct(), _msg("_a 확인하러 감: 무언가_")]
    start = conduct._round_start_index(messages, CONDUCTOR_ID)
    assert conduct._spoke_since(messages, start) == set()


# --------------------------------------------------------------------------
# composition line
# --------------------------------------------------------------------------

def test_the_composition_line_separates_seats_from_families():
    """"3/3" reads as three independent samples. Two of those seats sharing a
    vendor is exactly what that number hides, so print both counts."""
    line = conduct._composition({
        "a": {"family": "Anthropic", "verify": True},
        "b": {"family": "Anthropic", "verify": True},
        "c": {"family": "Google", "verify": False},
    })
    assert "좌석 3" in line
    assert "계열 2" in line
    assert "Anthropic 2" in line
    assert "확인 가능 2 / 불가 1" in line


def test_the_composition_line_counts_verifying_seats_separately():
    """A seat that cannot open the file is not a second confirmation of it."""
    line = conduct._composition({
        "a": {"family": "Anthropic", "verify": True},
        "b": {"family": "OpenAI", "verify": False},
    })
    assert "확인 가능 1 / 불가 1" in line


# --------------------------------------------------------------------------
# credential separation
# --------------------------------------------------------------------------

def test_the_conductor_refuses_to_start_on_the_seat_token(tmp_path, monkeypatch):
    """One token in both files collapses the separation the design rests on, and
    the symptom would be a 403 that reads like a Discord misconfiguration."""
    monkeypatch.delenv(conduct.CONDUCTOR_TOKEN_ENV_VAR, raising=False)
    monkeypatch.setattr(board_seat, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(conduct, "CONDUCTOR_ENV_PATH", tmp_path / ".board-conductor.env")
    monkeypatch.setattr(board_seat, "ENV_PATH", tmp_path / ".board.env")
    same = "DISCORD_BOT_TOKEN=aaa.bbb.ccc\n"
    (tmp_path / ".board-conductor.env").write_text(same, encoding="utf-8")
    (tmp_path / ".board.env").write_text(same, encoding="utf-8")

    with pytest.raises(conduct.BoardError) as e:
        conduct.conductor_client()
    assert "different bots" in str(e.value)


def test_the_conductor_and_seat_read_different_variables():
    """A shared DISCORD_BOT_TOKEN would let an exported conductor token promote
    every seat command run in the same shell."""
    assert conduct.CONDUCTOR_TOKEN_ENV_VAR != board_seat.TOKEN_ENV_VAR
    assert conduct.CONDUCTOR_ENV_PATH.name != board_seat.ENV_PATH.name
