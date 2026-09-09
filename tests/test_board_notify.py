"""What reaches a person's Discord, and what must not.

A room that reports every poll teaches the person to stop reading it, so the value here
is in the suppression: routine waiting is silent, an unchanged blocked stage announces
itself once, and the instruction lane is never written to at all.
"""
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


NOTIFY = load("board_notify", "board_notify.py")


@pytest.fixture()
def state(tmp_path: Path) -> Path:
    return tmp_path / "notify.json"


def _blocked(stage: str = "ack", who: str = "claude", why: str = "attention_required") -> dict:
    return {
        "round_id": "drive-1", "action": "collect_acknowledgements", "stage": stage,
        "applied": [], "waiting": [], "blocked": [{"participant": who, "why": why}],
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"round_id": "r", "action": "await_collection", "waiting": ["codex"]},
        {"round_id": "r", "action": "collect_acknowledgements", "applied": ["codex"],
         "waiting": ["claude"], "blocked": []},
        {"round_id": "r", "action": "none", "done": True},
    ],
)
def test_routine_progress_never_reaches_discord(payload, state: Path):
    result = NOTIFY.notify(payload, state_path=state, dry_run=True)

    assert result["sent"] is False
    assert result["reason"] == "not_notable"


@pytest.mark.parametrize(
    "payload, fragment",
    [
        ({"round_id": "r", "action": "finalized", "decided_at": "2026-09-08T00:00:00Z"},
         "합의 도달"),
        ({"round_id": "r", "action": "no_consensus", "code": "UNANIMOUS_APPROVAL_REQUIRED",
          "detail": "x"}, "합의 실패"),
        ({"round_id": "r", "action": "publish_proposal", "refs": [9]}, "승인 대기"),
        (_blocked(), "진행 막힘"),
    ],
)
def test_the_four_things_a_person_can_act_on_are_announced(payload, fragment, state: Path):
    result = NOTIFY.notify(payload, state_path=state, dry_run=True)

    assert result["reason"] == "dry_run"
    assert fragment in result["message"]


def test_an_unchanged_blocked_stage_announces_itself_once(state: Path):
    sent: list[str] = []
    first = NOTIFY.notify(_blocked(), state_path=state, now=1000.0, cooldown=3600.0,
                          post=sent.append)
    second = NOTIFY.notify(_blocked(), state_path=state, now=1200.0, cooldown=3600.0,
                           post=sent.append)

    assert first["sent"] is True
    assert second["sent"] is False and second["reason"] == "cooldown"
    assert len(sent) == 1


def test_a_second_seat_failing_is_still_news(state: Path):
    sent: list[str] = []
    NOTIFY.notify(_blocked(who="claude"), state_path=state, now=1000.0, post=sent.append)
    later = NOTIFY.notify(_blocked(who="codex"), state_path=state, now=1200.0,
                          post=sent.append)

    # The key carries who is blocking, so this is a different event, not a repeat.
    assert later["sent"] is True
    assert len(sent) == 2


def test_the_cooldown_expires(state: Path):
    sent: list[str] = []
    NOTIFY.notify(_blocked(), state_path=state, now=1000.0, post=sent.append)
    later = NOTIFY.notify(_blocked(), state_path=state, now=1000.0 + 3601, cooldown=3600.0,
                          post=sent.append)

    assert later["sent"] is True
    assert len(sent) == 2


def test_an_unreadable_ledger_does_not_swallow_an_alert(state: Path):
    state.write_text("{ this is not json", encoding="utf-8")

    result = NOTIFY.notify(_blocked(), state_path=state, dry_run=True)

    assert result["reason"] == "dry_run"


def test_the_conductor_lane_is_refused(tmp_path: Path):
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({"round_id": "r", "action": "finalized"}), encoding="utf-8")
    lines: list[str] = []

    code = NOTIFY.main(["--channel", "script", "--payload-file", str(payload)],
                       output=lines.append)

    assert code == 2
    assert json.loads(lines[0])["reason"] == "conductor_lane_refused"


def test_an_unreadable_payload_is_reported_not_guessed(tmp_path: Path):
    payload = tmp_path / "payload.json"
    payload.write_text("not json at all", encoding="utf-8")
    lines: list[str] = []

    code = NOTIFY.main(["--payload-file", str(payload)], output=lines.append)

    assert code == 2
    assert json.loads(lines[0])["reason"] == "unreadable_payload"


def test_goal_driver_attention_exposes_only_safe_transition_metadata(state: Path):
    payload = {
        "action": "goal_driver_attention",
        "goal_id": "release-v2",
        "run_id": 17,
        "error_code": "MODEL_TIMEOUT",
        "detail": "SECRET provider response body",
        "prompt": "SECRET manager prompt",
    }

    result = NOTIFY.notify(payload, state_path=state, dry_run=True)

    assert result["reason"] == "dry_run"
    assert "release-v2" in result["message"]
    assert "MODEL_TIMEOUT" in result["message"]
    assert "SECRET" not in result["message"]


def test_goal_task_completion_exposes_status_not_result_body(state: Path):
    payload = {
        "action": "goal_task_run_completed", "goal_id": "g", "task_id": "t",
        "run_id": 3, "result_status": "completed", "result": "SECRET work body",
    }
    result = NOTIFY.notify(payload, state_path=state, dry_run=True)
    assert result["reason"] == "dry_run"
    assert "g/t" in result["message"] and "completed" in result["message"]
    assert "SECRET" not in result["message"]


def test_goal_task_attention_exposes_only_error_code(state: Path):
    payload = {
        "action": "goal_task_run_attention", "goal_id": "g", "task_id": "t",
        "run_id": 4, "error_code": "MODEL_TIMEOUT", "detail": "SECRET failure body",
    }
    result = NOTIFY.notify(payload, state_path=state, dry_run=True)
    assert result["reason"] == "dry_run"
    assert "MODEL_TIMEOUT" in result["message"] and "SECRET" not in result["message"]
