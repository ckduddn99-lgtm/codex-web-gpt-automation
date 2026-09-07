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


def test_paste_mode_seats_the_room_without_any_launcher(plan):
    """The board does not care how a participant arrived, only that each arrived separately.

    Automating session creation drags in the model picker, the thinking-effort slider, an
    Oracle session lock held per project root, and a DevSpace root registration. Each of
    those has failed a launch before the first seat ever answered. Pasting costs one paste
    per seat and skips all of it.
    """
    text = SPLIT.render_invites(plan)

    for participant in plan["participants"]:
        assert f"===== SEAT: {participant}" in text
        assert f"--participant {participant}" in text
    assert "do not ask one session to answer as several seats" in text
    assert "seal --room-root" in text

    runtime = BOARD.open_room(Path(plan["room_root"]))
    for participant in plan["participants"]:
        assert BOARD.read_token_file(runtime.invites_path / f"{participant}.token") not in text


def test_a_launch_that_dies_on_a_preflight_returns_the_room_not_a_stack_trace(plan, monkeypatch, tmp_path):
    """Observed live: a DevSpace preflight error escaped as a traceback, burying the room id."""
    class PreflightError(RuntimeError):
        code = "DEVSPACE_EXACT_ROOT_UNAVAILABLE"

    def explode(_manifest_path, **_kwargs):
        raise PreflightError("the exact project root is not registered in DevSpace allowedRoots")

    monkeypatch.setattr(SPLIT.ORACLE_MULTI, "run_multi", explode)
    question = tmp_path / "q.md"
    question.write_text("q", encoding="utf-8")
    monkeypatch.setattr(SPLIT, "build_split_plan", lambda **_kwargs: plan)

    lines: list[str] = []
    code = SPLIT.main(
        ["2", "--project-root", str(tmp_path), "--question-file", str(question)],
        output=lines.append,
    )
    payload = json.loads("\n".join(lines))
    assert code == 2
    assert payload["ok"] is False
    assert payload["code"] == "DEVSPACE_EXACT_ROOT_UNAVAILABLE"
    assert payload["room_id"] == plan["room_id"]
    assert "--paste" in payload["hint"]


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


def test_a_launch_where_nothing_came_up_leaves_the_room_alone(plan, monkeypatch):
    """Observed live: every lane failed on a project lock before any browser opened.

    Withdrawing one seat at a time would drop seats until the two-participant floor
    refused the rest, and the survivors would then be reported as seated when no session
    exists for them at all. A total failure is a fact about the launch, not a roster
    change.
    """
    monkeypatch.setattr(
        SPLIT.ORACLE_MULTI, "run_multi",
        lambda _p, **_k: {
            "ok": False,
            "lanes": [{"id": f"seat{i}", "ok": False} for i in (1, 2, 3, 4)],
            "session_locators": [],
        },
    )
    report = SPLIT.run_split(plan)

    assert report["status"] == "no_seat_came_up"
    assert report["seated"] == []
    assert report["withdrawn"] == []
    assert report["failed_seats"] == ["seat1", "seat2", "seat3", "seat4"]

    # The room is untouched, so a retry does not inherit a half-withdrawn roster.
    runtime = BOARD.open_room(Path(plan["room_root"]))
    assert BOARD.status(runtime)["participants"] == ["seat1", "seat2", "seat3", "seat4"]
    assert BOARD.status(runtime)["withdrawn"] == []


def test_dry_run_withdraws_nobody(plan, monkeypatch):
    monkeypatch.setattr(
        SPLIT.ORACLE_MULTI, "run_multi",
        lambda _p, **_k: {"lanes": [{"id": "seat2", "ok": False}], "session_locators": []},
    )
    report = SPLIT.run_split(plan, dry_run=True)
    assert report["withdrawn"] == []
    assert report["seated"] == ["seat1", "seat2", "seat3", "seat4"]
