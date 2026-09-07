"""The board's whole value is the gate, so most of these are negative tests.

If a participant can reach another participant's answer before the seal, running four
sessions instead of one bought nothing: the first answer anchors the rest and the
independence that justified the cost is gone. So "you cannot read yet" is asserted from
every direction it could leak - the read API, the long-poll, the reply path, and the
bytes on disk in the shared room.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "chatgpt_meeting_board.py"


def load_module():
    name = "chatgpt_meeting_board_test"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


BOARD = load_module()


@pytest.fixture()
def board(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    private = tmp_path / "host-state"
    runtime, tokens = BOARD.create_room(
        project,
        participants=["alpha", "bravo", "charlie"],
        question="Does the epoch gate hold for live funds?",
        room_id="board-test-room",
        private_root=private,
    )
    return runtime, tokens


def submit_all(runtime, tokens, *, texts=None):
    texts = texts or {name: f"{name} independent answer" for name in tokens}
    for name, token in tokens.items():
        BOARD.submit(runtime, participant=name, token=token, text=texts[name])
    return texts


# --- registration -----------------------------------------------------------------


def test_roster_is_closed_at_creation(board):
    runtime, tokens = board
    assert sorted(tokens) == ["alpha", "bravo", "charlie"]
    assert BOARD.status(runtime)["phase"] == BOARD.PHASE_COLLECT


def test_duplicate_participant_is_refused(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(BOARD.MeetingBoardError) as excinfo:
        BOARD.create_room(
            project,
            participants=["alpha", "alpha"],
            question="q",
            room_id="board-dup-room",
            private_root=tmp_path / "state",
        )
    assert excinfo.value.code == "PARTICIPANT_DUPLICATE"


def test_a_board_of_one_is_refused(tmp_path: Path):
    """One participant is not a meeting; sealing it would prove nothing about anchoring."""
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(BOARD.MeetingBoardError) as excinfo:
        BOARD.create_room(
            project,
            participants=["alpha"],
            question="q",
            room_id="board-solo-room",
            private_root=tmp_path / "state",
        )
    assert excinfo.value.code == "PARTICIPANT_COUNT_INVALID"


def test_unknown_participant_and_wrong_token_are_both_refused(board):
    runtime, tokens = board
    with pytest.raises(BOARD.MeetingBoardError) as unknown:
        BOARD.submit(runtime, participant="delta", token=tokens["alpha"], text="x")
    assert unknown.value.code == "PARTICIPANT_UNKNOWN"
    with pytest.raises(BOARD.MeetingBoardError) as mismatch:
        BOARD.submit(runtime, participant="bravo", token=tokens["alpha"], text="x")
    assert mismatch.value.code == "TOKEN_MISMATCH"


def test_invite_stores_only_the_token_digest(board):
    runtime, tokens = board
    stored = json.loads((runtime.invites_path / "alpha.json").read_text(encoding="utf-8"))
    assert "token" not in stored
    assert tokens["alpha"] not in json.dumps(stored)


# --- phase 1: write-only ------------------------------------------------------------


def test_submission_text_never_lands_in_the_shared_room(board):
    """The gate is this: the bytes are not where a rival is told to look."""
    runtime, tokens = board
    BOARD.submit(runtime, participant="alpha", token=tokens["alpha"], text="alpha secret finding")

    shared = [p for p in runtime.root.rglob("*") if p.is_file()]
    assert shared, "the room should not be empty"
    for path in shared:
        assert "alpha secret finding" not in path.read_text(encoding="utf-8", errors="replace")

    receipt = json.loads((runtime.receipts_path / "alpha.json").read_text(encoding="utf-8"))
    assert receipt["text_sha256"] == BOARD._sha256(b"alpha secret finding")
    assert "text" not in receipt


def test_reading_the_bundle_before_the_seal_is_refused(board):
    runtime, tokens = board
    BOARD.submit(runtime, participant="alpha", token=tokens["alpha"], text="alpha answer")
    with pytest.raises(BOARD.MeetingBoardError) as excinfo:
        BOARD.read_bundle(runtime, participant="bravo", token=tokens["bravo"])
    assert excinfo.value.code == "PHASE_NOT_OPEN"


def test_long_poll_during_collect_never_carries_another_answer(board):
    runtime, tokens = board
    BOARD.submit(runtime, participant="alpha", token=tokens["alpha"], text="alpha answer")
    result = BOARD.watch(
        runtime,
        participant="bravo",
        token=tokens["bravo"],
        after=0,
        wait_seconds=0.0,
        sleep=lambda _seconds: None,
    )
    assert result["status"] == "collecting"
    assert result["message"] is None
    assert "alpha answer" not in json.dumps(result, ensure_ascii=False)
    # It may say who it is still waiting on - that is the room's shape, not an answer.
    assert result["pending"] == ["bravo", "charlie"]


def test_double_submission_is_refused(board):
    runtime, tokens = board
    BOARD.submit(runtime, participant="alpha", token=tokens["alpha"], text="first")
    with pytest.raises(BOARD.MeetingBoardError) as excinfo:
        BOARD.submit(runtime, participant="alpha", token=tokens["alpha"], text="second")
    assert excinfo.value.code == "SUBMISSION_DUPLICATE"


def test_replies_and_issues_are_refused_before_the_seal(board):
    runtime, tokens = board
    with pytest.raises(BOARD.MeetingBoardError) as issue:
        BOARD.open_issue(runtime, issue_id="conflict-1", summary="s", participants=["alpha", "bravo"])
    assert issue.value.code == "PHASE_NOT_OPEN"
    with pytest.raises(BOARD.MeetingBoardError) as reply:
        BOARD.reply(
            runtime,
            participant="alpha",
            token=tokens["alpha"],
            issue_id="conflict-1",
            text="early",
        )
    assert reply.value.code == "PHASE_NOT_OPEN"


# --- the seal -----------------------------------------------------------------------


def test_partial_seal_is_refused_and_names_who_is_missing(board):
    runtime, tokens = board
    BOARD.submit(runtime, participant="alpha", token=tokens["alpha"], text="alpha answer")
    with pytest.raises(BOARD.MeetingBoardError) as excinfo:
        BOARD.seal(runtime)
    assert excinfo.value.code == "SUBMISSIONS_INCOMPLETE"
    assert excinfo.value.evidence["pending"] == ["bravo", "charlie"]


def test_seal_commits_to_exact_bytes_and_then_opens(board):
    runtime, tokens = board
    texts = submit_all(runtime, tokens)
    result = BOARD.seal(runtime)

    published = (runtime.sealed_path / "bundle.json").read_bytes().rstrip(b"\n")
    assert BOARD._sha256(published) == result["bundle_sha256"]
    assert (runtime.sealed_path / "bundle.sha256").read_text(encoding="ascii").strip() == result["bundle_sha256"]
    assert BOARD.status(runtime)["phase"] == BOARD.PHASE_OPEN

    bundle = json.loads(published.decode("utf-8"))
    assert [entry["participant_id"] for entry in bundle["entries"]] == ["alpha", "bravo", "charlie"]
    for entry in bundle["entries"]:
        assert entry["text"] == texts[entry["participant_id"]]


def test_seal_refuses_a_submission_that_no_longer_matches_its_receipt(board):
    """The receipt was public during phase 1 and the text was not, so they must still agree."""
    runtime, tokens = board
    submit_all(runtime, tokens)
    stored = runtime.submissions_path / "alpha.json"
    tampered = json.loads(stored.read_text(encoding="utf-8"))
    tampered["text"] = "rewritten after seeing the others"
    # Rewrite the digest too: anything able to edit the text could edit the field beside
    # it, so the check has to re-hash rather than trust what is stored.
    tampered["text_sha256"] = BOARD._sha256(tampered["text"].encode("utf-8"))
    stored.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(BOARD.MeetingBoardError) as excinfo:
        BOARD.seal(runtime)
    assert excinfo.value.code == "SUBMISSION_TAMPERED"


def test_submitting_after_the_seal_is_refused(board):
    runtime, tokens = board
    submit_all(runtime, tokens)
    BOARD.seal(runtime)
    (runtime.receipts_path / "alpha.json").unlink()
    with pytest.raises(BOARD.MeetingBoardError) as excinfo:
        BOARD.submit(runtime, participant="alpha", token=tokens["alpha"], text="late answer")
    assert excinfo.value.code == "PHASE_CLOSED"


# --- phase 2: scoped cross-examination ----------------------------------------------


def test_bundle_is_readable_only_after_the_seal(board):
    runtime, tokens = board
    texts = submit_all(runtime, tokens)
    BOARD.seal(runtime)
    bundle = BOARD.read_bundle(runtime, participant="bravo", token=tokens["bravo"])
    assert {entry["text"] for entry in bundle["entries"]} == set(texts.values())


def test_a_tampered_bundle_is_not_served(board):
    runtime, tokens = board
    submit_all(runtime, tokens)
    BOARD.seal(runtime)
    (runtime.sealed_path / "bundle.json").write_text('{"schema":"x"}', encoding="utf-8")
    with pytest.raises(BOARD.MeetingBoardError) as excinfo:
        BOARD.read_bundle(runtime, participant="bravo", token=tokens["bravo"])
    assert excinfo.value.code == "BUNDLE_TAMPERED"


def test_an_issue_needs_two_sides(board):
    runtime, tokens = board
    submit_all(runtime, tokens)
    BOARD.seal(runtime)
    with pytest.raises(BOARD.MeetingBoardError) as excinfo:
        BOARD.open_issue(runtime, issue_id="solo", summary="s", participants=["alpha"])
    assert excinfo.value.code == "ISSUE_NOT_A_CONFLICT"


def test_reply_requires_an_open_issue_that_names_the_author(board):
    runtime, tokens = board
    submit_all(runtime, tokens)
    BOARD.seal(runtime)
    with pytest.raises(BOARD.MeetingBoardError) as unknown:
        BOARD.reply(runtime, participant="alpha", token=tokens["alpha"], issue_id="ghost", text="x")
    assert unknown.value.code == "ISSUE_UNKNOWN"

    BOARD.open_issue(runtime, issue_id="cost-model", summary="alpha and bravo disagree",
                     participants=["alpha", "bravo"])
    with pytest.raises(BOARD.MeetingBoardError) as outsider:
        BOARD.reply(runtime, participant="charlie", token=tokens["charlie"], issue_id="cost-model", text="x")
    assert outsider.value.code == "ISSUE_NOT_ADDRESSED"


def test_reply_preserves_the_relation_that_handoff_files_lose(board):
    runtime, tokens = board
    submit_all(runtime, tokens)
    BOARD.seal(runtime)
    BOARD.open_issue(runtime, issue_id="cost-model", summary="alpha and bravo disagree",
                     participants=["alpha", "bravo"])

    first = BOARD.reply(runtime, participant="alpha", token=tokens["alpha"], issue_id="cost-model",
                        text="you read line 42 as gross")
    second = BOARD.reply(runtime, participant="bravo", token=tokens["bravo"], issue_id="cost-model",
                         text="I did, and here is why", reply_to=first["id"])

    assert first["author"] == "alpha" and first["reply_to"] is None
    assert second["author"] == "bravo" and second["reply_to"] == first["id"]
    for record in (first, second):
        assert record["phase"] == BOARD.PHASE_OPEN
        assert record["issue_id"] == "cost-model"
        assert record["text_sha256"] == BOARD._sha256(record["text"].encode("utf-8"))
        assert record["created_at"].endswith("Z")


def test_reply_to_must_name_a_reply_that_exists(board):
    runtime, tokens = board
    submit_all(runtime, tokens)
    BOARD.seal(runtime)
    BOARD.open_issue(runtime, issue_id="cost-model", summary="s", participants=["alpha", "bravo"])
    with pytest.raises(BOARD.MeetingBoardError) as excinfo:
        BOARD.reply(runtime, participant="alpha", token=tokens["alpha"], issue_id="cost-model",
                    text="x", reply_to=99)
    assert excinfo.value.code == "REPLY_TO_UNKNOWN"


def test_watch_delivers_replies_in_order_after_the_seal(board):
    runtime, tokens = board
    submit_all(runtime, tokens)
    BOARD.seal(runtime)
    BOARD.open_issue(runtime, issue_id="cost-model", summary="s", participants=["alpha", "bravo"])
    BOARD.reply(runtime, participant="alpha", token=tokens["alpha"], issue_id="cost-model", text="one")
    BOARD.reply(runtime, participant="bravo", token=tokens["bravo"], issue_id="cost-model", text="two")

    first = BOARD.watch(runtime, participant="charlie", token=tokens["charlie"], after=0,
                        wait_seconds=0.0, sleep=lambda _s: None)
    # The phase rides on every response, so charlie learns the room opened from the
    # same payload that carries reply #1 - no notification message for a cursor to skip.
    assert first["status"] == "reply" and first["cursor"] == 1
    assert first["phase"] == BOARD.PHASE_OPEN and first["bundle_sha256"]
    assert first["message"]["text"] == "one"

    nxt = BOARD.watch(runtime, participant="charlie", token=tokens["charlie"], after=first["cursor"],
                      wait_seconds=0.0, sleep=lambda _s: None)
    assert nxt["status"] == "reply" and nxt["message"]["text"] == "two" and nxt["cursor"] == 2

    drained = BOARD.watch(runtime, participant="charlie", token=tokens["charlie"], after=nxt["cursor"],
                          wait_seconds=0.0, sleep=lambda _s: None)
    assert drained["status"] == "open" and drained["message"] is None


# --- operator surface ---------------------------------------------------------------


def test_attach_prompt_carries_the_utf8_flag_and_forbids_fake_children(board):
    """Windows stdin mangled Korean into surrogates without -X utf8 in the live canary."""
    runtime, tokens = board
    prompt = BOARD.build_attach_prompt(runtime, participant="alpha", token=tokens["alpha"],
                                       python_executable="python")
    assert "-X utf8" in prompt
    assert "sub-agents" in prompt
    assert str(runtime.root) in prompt
    assert "Does the epoch gate hold for live funds?" in prompt


def test_cli_reports_a_refusal_as_structured_json(board, capsys):
    runtime, tokens = board
    lines: list[str] = []
    code = BOARD.main(
        ["read-bundle", "--room-root", str(runtime.root), "--participant", "alpha",
         "--token", tokens["alpha"]],
        output=lines.append,
    )
    assert code == 2
    payload = json.loads("\n".join(lines))
    assert payload == {
        "ok": False,
        "code": "PHASE_NOT_OPEN",
        "error": "other participants' answers are not readable until every answer is sealed",
        "evidence": {},
    }
