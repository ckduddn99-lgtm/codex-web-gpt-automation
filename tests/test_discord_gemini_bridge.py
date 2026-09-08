"""The person's lane: one question, one answer, never twice and never silently."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


BIN = Path(__file__).resolve().parents[1] / "bin"


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, BIN / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


BRIDGE = load("discord_gemini_bridge", "discord_gemini_bridge.py")


class FakeClient:
    """Enough of the Discord client to exercise the lanes."""

    def __init__(self, script_messages: list[dict]):
        self._script = script_messages
        self.posted: list[tuple[str, str]] = []

    def messages_after(self, channel_id: str, after: str | None, limit: int = 100):
        if after is None:
            return list(self._script)
        seen = [row["id"] for row in self._script]
        return self._script[seen.index(after) + 1:] if after in seen else list(self._script)

    def post(self, channel_id: str, content: str):
        self.posted.append((channel_id, content))
        return [{"id": f"posted-{len(self.posted)}"}]


def _message(mid: str, content: str, *, bot: bool = False) -> dict:
    return {"id": mid, "content": content, "author": {"username": "u", "bot": bot}}


@pytest.fixture(autouse=True)
def _channels(monkeypatch):
    monkeypatch.setattr(BRIDGE.SEAT, "resolve_guild", lambda client, name=None: {"id": "g"})
    monkeypatch.setattr(
        BRIDGE.SEAT, "resolve_channel",
        lambda client, guild_id, name: {"id": f"chan-{name}", "name": name},
    )


def _poll(client, state: Path, ask, **kwargs):
    return BRIDGE.poll_once(
        agy=Path("agy"), client=client, ask=ask, state_path=state,
        provider_lock=state.parent / "provider.lock", **kwargs,
    )


def test_a_question_is_answered_in_the_general_channel(tmp_path: Path):
    client = FakeClient([_message("1", "오늘 뭐부터 해야 해?")])

    result = _poll(client, tmp_path / "s.json", lambda prompt: (True, "P0부터."))

    assert result == {"answered": 1, "reason": "ok", "skipped": 0}
    assert client.posted == [("chan-일반", "P0부터.")]


def test_the_same_question_is_not_answered_twice(tmp_path: Path):
    state = tmp_path / "s.json"
    client = FakeClient([_message("1", "질문")])
    _poll(client, state, lambda prompt: (True, "답"))

    again = _poll(client, state, lambda prompt: (True, "또 답"))

    assert again["reason"] == "nothing_new"
    assert len(client.posted) == 1


def test_a_bot_message_is_not_treated_as_a_person_asking(tmp_path: Path):
    client = FakeClient([_message("1", "지휘 알림입니다", bot=True)])

    result = _poll(client, tmp_path / "s.json", lambda prompt: (True, "답"))

    assert result["reason"] == "nothing_new"
    assert client.posted == []


def test_a_failure_says_so_and_does_not_retry(tmp_path: Path):
    state = tmp_path / "s.json"
    client = FakeClient([_message("1", "질문")])

    result = _poll(client, state, lambda prompt: (False, "모델이 빈 응답을 돌려줬습니다."))

    assert result["reason"] == "model_failed"
    assert "답변 실패" in client.posted[0][1]
    # The cursor moved past it, so the next tick will not call the model again for a
    # request whose work may already have happened.
    calls: list[str] = []

    def _record(prompt):
        calls.append(prompt)
        return True, "두 번째"

    assert _poll(client, state, _record)["reason"] == "nothing_new"
    assert calls == []


def test_a_busy_provider_slot_waits_without_losing_the_question(tmp_path: Path):
    state = tmp_path / "s.json"
    lock = state.parent / "provider.lock"
    client = FakeClient([_message("1", "질문")])

    with BRIDGE.BUS.provider_slot(lock) as held:
        assert held
        blocked = _poll(client, state, lambda prompt: (True, "답"))

    assert blocked == {"answered": 0, "reason": "provider_busy", "waiting": 1}
    assert client.posted == []
    # Once the slot frees, the same question is still there.
    assert _poll(client, state, lambda prompt: (True, "답"))["answered"] == 1


def test_the_bridge_never_answers_into_the_instruction_lane():
    lines: list[str] = []

    code = BRIDGE.main(["--answer-channel", "script"], output=lines.append)

    assert code == 2
    assert json.loads(lines[0])["reason"] == "conductor_lane_refused"


def test_only_a_bounded_number_of_questions_run_per_tick(tmp_path: Path):
    client = FakeClient([_message(str(i), f"질문 {i}") for i in range(1, 6)])

    result = _poll(client, tmp_path / "s.json", lambda prompt: (True, "답"), max_messages=2)

    assert result["answered"] == 2 and result["skipped"] == 3
    assert len(client.posted) == 2
