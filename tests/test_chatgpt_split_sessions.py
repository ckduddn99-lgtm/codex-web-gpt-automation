"""`split N` is one gesture that must produce N *separate* sessions, not one session
asked to play N parts, and it must not deadlock the room when one of them fails to come
up. Both of those are asserted here against a fake runner, because the real one needs a
browser."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "chatgpt_split_sessions.py"


def load_module():
    name = "chatgpt_split_sessions_test"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SPLIT = load_module()
BOARD = SPLIT.BOARD


@pytest.fixture()
def plan(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    return SPLIT.build_split_plan(
        project_root=project,
        question="Does the epoch gate hold for live funds?",
        seats=4,
        output_dir=project / ".workflow" / "split",
        room_id="split-test-room",
        private_root=tmp_path / "host-state",
    )


def test_split_seats_every_session_on_one_room(plan):
    assert plan["participants"] == ["seat1", "seat2", "seat3", "seat4"]
    runtime = BOARD.open_room(Path(plan["room_root"]))
    assert BOARD.status(runtime)["participants"] == plan["participants"]


def test_each_seat_gets_its_own_mission_and_no_two_share_a_credential(plan):
    """One session playing four parts is the failure mode; four missions is the fix."""
    manifest = json.loads(Path(plan["manifest_path"]).read_text(encoding="utf-8"))
    mission_paths = [lane["mission_path"] for lane in manifest["solvers"]]
    assert len(set(mission_paths)) == 4

    credentials = set()
    for lane in manifest["solvers"]:
        text = Path(lane["mission_path"]).read_text(encoding="utf-8")
        assert f"--participant {lane['id']}" in text
        marker = "--token-file "
        credentials.add(text.split(marker, 1)[1].split()[0])
    assert len(credentials) == 4


def test_missions_reference_tokens_and_never_carry_them(plan):
    runtime = BOARD.open_room(Path(plan["room_root"]))
    for participant in plan["participants"]:
        token = BOARD.read_token_file(runtime.invites_path / f"{participant}.token")
        for lane_mission in Path(plan["output_dir"], "missions").glob("*.md"):
            assert token not in lane_mission.read_text(encoding="utf-8")


def test_seats_open_one_at_a_time_by_default(plan):
    """Opening several at once has failed together before, so the waves are singletons."""
    assert plan["max_concurrency"] == 1
    assert [wave["lane_ids"] for wave in plan["waves"]] == [["seat1"], ["seat2"], ["seat3"], ["seat4"]]


def test_the_manifest_is_the_existing_runner_contract(plan):
    manifest = json.loads(Path(plan["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["schema"] == SPLIT.ORACLE_MULTI.SCHEMA
    assert manifest["model_strategy"] == "select"
    assert Path(manifest["merger_mission_path"]).is_file()


def test_the_synthesis_mission_forbids_manufacturing_a_conflict(plan):
    text = Path(json.loads(Path(plan["manifest_path"]).read_text(encoding="utf-8"))["merger_mission_path"]).read_text(
        encoding="utf-8"
    )
    assert "Convergence is a result, not a failure" in text
    assert "Do not count votes" in text


def test_a_seat_count_outside_the_range_is_refused(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(SPLIT.SplitError):
        SPLIT.build_split_plan(
            project_root=project,
            question="q",
            seats=1,
            output_dir=project / "out",
            room_id="split-solo-room",
            private_root=tmp_path / "state",
        )


def test_a_session_that_never_comes_up_is_withdrawn_instead_of_freezing_the_room(plan, monkeypatch):
    """One broken browser turn used to make the whole run unusable. Now it costs one seat."""
    def fake_run_multi(_manifest_path, **_kwargs):
        return {
            "lanes": [
                {"id": "seat1", "ok": True},
                {"id": "seat2", "ok": False},
                {"id": "seat3", "ok": True},
                {"id": "seat4", "ok": True},
            ],
            "session_locators": ["a", "b", "c"],
            "submitted": True,
        }

    monkeypatch.setattr(SPLIT.ORACLE_MULTI, "run_multi", fake_run_multi)
    report = SPLIT.run_split(plan)

    assert report["requested_seats"] == 4
    assert report["seated"] == ["seat1", "seat3", "seat4"]
    assert [row["participant_id"] for row in report["withdrawn"]] == ["seat2"]

    runtime = BOARD.open_room(Path(plan["room_root"]))
    for participant in report["seated"]:
        BOARD.submit(runtime, participant=participant,
                     token=BOARD.read_token_file(runtime.invites_path / f"{participant}.token"),
                     text=f"{participant} answer")
    sealed = BOARD.seal(runtime)
    assert sealed["entries"] == 3
    bundle = BOARD.read_bundle(runtime, participant="seat1",
                               token=BOARD.read_token_file(runtime.invites_path / "seat1.token"))
    assert bundle["withdrawn"][0]["participant_id"] == "seat2"


def test_a_seat_that_already_answered_is_reported_not_forced_out(plan, monkeypatch):
    """A late-reported lane failure must never silently drop an answer somebody gave."""
    runtime = BOARD.open_room(Path(plan["room_root"]))
    BOARD.submit(runtime, participant="seat2",
                 token=BOARD.read_token_file(runtime.invites_path / "seat2.token"),
                 text="seat2 answered before the runner reported")

    monkeypatch.setattr(
        SPLIT.ORACLE_MULTI, "run_multi",
        lambda _p, **_k: {"lanes": [{"id": "seat2", "ok": False}], "session_locators": []},
    )
    report = SPLIT.run_split(plan)

    assert report["withdrawn"] == [{"participant_id": "seat2", "withdraw_refused": "SUBMISSION_ALREADY_IN"}]
    assert "seat2" in report["seated"]


def test_dry_run_withdraws_nobody(plan, monkeypatch):
    monkeypatch.setattr(
        SPLIT.ORACLE_MULTI, "run_multi",
        lambda _p, **_k: {"lanes": [{"id": "seat2", "ok": False}], "session_locators": []},
    )
    report = SPLIT.run_split(plan, dry_run=True)
    assert report["withdrawn"] == []
    assert report["seated"] == ["seat1", "seat2", "seat3", "seat4"]
