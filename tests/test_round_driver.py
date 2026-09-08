"""The consensus barriers, driven end to end and then attacked.

The happy path matters least here. What these check is that the driver refuses to move
on anything it was told not to treat as agreement: a seat that said nothing, a seat
that failed, a seat that wrote prose instead of a decision, an objection its owner has
not closed, and a proposal somebody voted against.
"""
from __future__ import annotations

import importlib.util
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


BUS = load("chatgpt_server_bus", "chatgpt_server_bus.py")
DRIVER = load("round_driver", "round_driver.py")

SEATS = ["chatgpt", "claude", "codex"]
CONDUCTOR = "gemini"


@pytest.fixture()
def sealed(tmp_path: Path) -> Path:
    path = tmp_path / "bus.sqlite3"
    BUS.create_round(
        path, round_id="drive-1", sender=CONDUCTOR, participants=SEATS,
        question="Should the release gate block on a missing provenance field?",
    )
    for seat in SEATS:
        task = BUS.claim(path, recipient=seat, worker_id=f"w-{seat}")
        BUS.complete(path, task_id=task["task_id"], recipient=seat,
                     lease_token=task["lease_token"], result=f"{seat}: yes, it should.")
    return path


def _answer(path: Path, seat: str, text: str, *, fail: bool = False) -> None:
    """Stand in for the worker: claim whatever is addressed to this seat and reply."""
    task = BUS.claim(path, recipient=seat, worker_id=f"w-{seat}")
    assert task is not None, f"nothing was addressed to {seat}"
    if fail:
        BUS.attention(path, task_id=task["task_id"], recipient=seat,
                      lease_token=task["lease_token"], error=text)
        return
    BUS.complete(path, task_id=task["task_id"], recipient=seat,
                 lease_token=task["lease_token"], result=text)


def _advance(path: Path) -> dict:
    return DRIVER.advance(path, round_id="drive-1", conductor=CONDUCTOR)


def _digest(path: Path) -> str:
    return BUS.bundle(path, round_id="drive-1")["bundle_sha256"]


def _drive_to_consensus(path: Path, *, votes: dict[str, str] | None = None) -> dict:
    votes = votes or {seat: "APPROVE" for seat in SEATS}
    _advance(path)                                              # issue ack tasks
    for seat in SEATS:
        _answer(path, seat, f"I read them.\nACK {_digest(path)}")
    _advance(path)                                              # apply acks
    _advance(path)                                              # issue review tasks
    for seat in SEATS:
        _answer(path, seat, "Nothing to add.\nREVIEW_COMPLETE")
    _advance(path)                                              # apply reviews
    _advance(path)                                              # issue proposal task
    _answer(path, CONDUCTOR, "The gate blocks. Provenance is the sample's entrance.")
    _advance(path)                                              # publish proposal
    _advance(path)                                              # issue vote tasks
    for seat in SEATS:
        _answer(path, seat, f"Considered.\n{votes[seat]}")
    _advance(path)                                              # apply votes
    return _advance(path)                                       # finalize attempt


def test_a_round_reaches_consensus_through_every_barrier(sealed: Path):
    outcome = _drive_to_consensus(sealed)

    assert outcome["action"] == "finalized"
    assert BUS.status(sealed, round_id="drive-1")["phase"] == "consensus"


def test_a_silent_seat_is_waited_for_and_never_counted(sealed: Path):
    _advance(sealed)
    _answer(sealed, "chatgpt", f"ACK {_digest(sealed)}")
    # claude and codex simply have not answered.

    outcome = _advance(sealed)

    assert outcome["action"] == "collect_acknowledgements"
    assert outcome["applied"] == ["chatgpt"]
    assert outcome["waiting"] == ["claude", "codex"]
    assert not outcome["blocked"]
    receipts = BUS.status(sealed, round_id="drive-1")["read_receipts"]
    assert receipts["received"] == 1 and receipts["required"] == 3


def test_a_failed_seat_blocks_its_stage_and_is_not_retried(sealed: Path):
    _advance(sealed)
    _answer(sealed, "chatgpt", f"ACK {_digest(sealed)}")
    _answer(sealed, "claude", "provider timed out", fail=True)
    _answer(sealed, "codex", f"ACK {_digest(sealed)}")

    outcome = _advance(sealed)

    assert [row["participant"] for row in outcome["blocked"]] == ["claude"]
    assert outcome["blocked"][0]["why"] == "attention_required"
    # The stage does not silently reissue work that may already have run.
    assert BUS.claim(sealed, recipient="claude", worker_id="retry") is None


def test_prose_instead_of_a_decision_is_not_an_acknowledgement(sealed: Path):
    _advance(sealed)
    _answer(sealed, "chatgpt", "I have read the bundle and I broadly agree with it.")
    _answer(sealed, "claude", f"ACK {_digest(sealed)}")
    _answer(sealed, "codex", f"ACK {_digest(sealed)}")

    outcome = _advance(sealed)

    assert outcome["applied"] == ["claude", "codex"]
    assert outcome["blocked"][0]["participant"] == "chatgpt"
    assert outcome["blocked"][0]["why"] == "unrecognised_acknowledgement"


def test_acknowledging_the_wrong_digest_is_not_reading_the_bundle(sealed: Path):
    _advance(sealed)
    _answer(sealed, "chatgpt", "ACK " + "0" * 64)

    outcome = _advance(sealed)

    assert outcome["blocked"][0]["why"] == "unrecognised_acknowledgement"
    assert BUS.status(sealed, round_id="drive-1")["read_receipts"]["received"] == 0


def test_an_open_objection_blocks_the_proposal_until_its_owner_closes_it(sealed: Path):
    _advance(sealed)
    for seat in SEATS:
        _answer(sealed, seat, f"ACK {_digest(sealed)}")
    _advance(sealed)
    _advance(sealed)
    _answer(sealed, "chatgpt", "OBJECT the sample window is not stated")
    _answer(sealed, "claude", "REVIEW_COMPLETE")
    _answer(sealed, "codex", "REVIEW_COMPLETE")
    _advance(sealed)

    outcome = _advance(sealed)
    assert outcome["action"] == "resolve_objections"
    assert outcome["waiting"] == ["chatgpt"]

    # The owner is not satisfied: the round stays exactly where it is.
    _answer(sealed, "chatgpt", "STILL_OPEN")
    held = _advance(sealed)
    assert held["action"] == "resolve_objections"
    assert BUS.status(sealed, round_id="drive-1")["open_issues"] == 1
    with pytest.raises(BUS.BusError) as refused:
        BUS.propose(sealed, round_id="drive-1", sender=CONDUCTOR, proposal="anything")
    assert refused.value.code == "OPEN_ISSUES"


def test_a_single_rejection_denies_consensus(sealed: Path):
    outcome = _drive_to_consensus(
        sealed, votes={"chatgpt": "APPROVE", "claude": "REJECT the window is still unstated",
                       "codex": "APPROVE"},
    )

    assert outcome["action"] == "no_consensus"
    assert outcome["code"] == "UNANIMOUS_APPROVAL_REQUIRED"
    assert BUS.status(sealed, round_id="drive-1")["phase"] == "sealed"


def test_abstaining_is_not_approval(sealed: Path):
    outcome = _drive_to_consensus(
        sealed, votes={"chatgpt": "APPROVE", "claude": "ABSTAIN", "codex": "APPROVE"},
    )

    assert outcome["action"] == "no_consensus"
    assert outcome["code"] == "UNANIMOUS_APPROVAL_REQUIRED"


def test_advance_is_idempotent_so_a_timer_may_call_it_repeatedly(sealed: Path):
    first = _advance(sealed)
    second = _advance(sealed)

    assert first["action"] == second["action"] == "collect_acknowledgements"
    # One task per seat per stage, no matter how often the driver runs.
    stages = [row["stage"] for row in BUS.status(sealed, round_id="drive-1")["tasks"]]
    assert stages.count("ack") == len(SEATS)


def test_the_driver_waits_while_the_seats_are_still_answering(tmp_path: Path):
    path = tmp_path / "bus.sqlite3"
    BUS.create_round(path, round_id="drive-1", sender=CONDUCTOR, participants=SEATS,
                     question="Still collecting.")
    BUS.claim(path, recipient="chatgpt", worker_id="w1")

    outcome = _advance(path)

    assert outcome["action"] == "await_collection"
    assert sorted(outcome["waiting"]) == SEATS


def test_the_acknowledgement_shows_the_answers_not_a_list_of_refs(sealed: Path):
    """Both real seats refused the first version of this instruction, and were right.

    bundle() returns refs because each body is stored once. Passing that through showed
    every seat "(no body)" and asked it to attest to a digest over content it could not
    read, which is a request to rubber-stamp.
    """
    payload = BUS.bundle(sealed, round_id="drive-1")

    text = DRIVER._ack_instruction(sealed, "drive-1", payload)

    assert "(no body)" not in text
    for seat in SEATS:
        assert f"{seat}: yes, it should." in text
    assert payload["bundle_sha256"] in text


def test_the_task_is_fenced_apart_from_the_material_it_evaluates(sealed: Path):
    """A seat rejected the earlier packet for embedding its orders inside the evidence.

    That is the shape this system distrusts everywhere else, so the instruction comes
    first and both halves are labelled. The seat should never have to infer which
    sentences were the job.
    """
    payload = BUS.bundle(sealed, round_id="drive-1")

    text = DRIVER._ack_instruction(sealed, "drive-1", payload)
    instruction, _, material = text.partition(DRIVER.INSTRUCTION_CLOSE)

    assert text.startswith(DRIVER.INSTRUCTION_OPEN)
    assert f"ACK {payload['bundle_sha256']}" in instruction
    # The seats' answers are evidence, never part of the order.
    assert "yes, it should." not in instruction
    assert DRIVER.MATERIAL_OPEN in material and "yes, it should." in material


def test_the_acknowledgement_does_not_claim_the_seat_verified_the_hash(sealed: Path):
    """Codex refused the earlier wording, and it was right: copying a handed-over hash
    proves neither reading nor computation. The digest names which bundle was read."""
    payload = BUS.bundle(sealed, round_id="drive-1")

    text = DRIVER._ack_instruction(sealed, "drive-1", payload)

    assert "계산했다는 증명이 아닙니다" in text
    assert "어느 묶음을 읽었는지 특정" in text


def test_active_rounds_finds_what_a_timer_must_drive(sealed: Path):
    """A timer cannot be configured with a round id.

    Rounds are created after the timer is installed, so anything driven by a fixed id
    would silently ignore every round made later.
    """
    BUS.create_round(sealed, round_id="drive-2", sender=CONDUCTOR, participants=SEATS,
                     question="A second question.")
    BUS.create_round(sealed, round_id="not-ours", sender="codex", participants=SEATS,
                     question="Someone else's round.")

    mine = DRIVER.active_rounds(sealed, conductor=CONDUCTOR)

    assert mine == ["drive-1", "drive-2"]
    # Another conductor's round is not this timer's to move.
    assert "not-ours" not in mine


def test_a_finished_round_stops_being_driven(sealed: Path):
    _drive_to_consensus(sealed)

    assert DRIVER.active_rounds(sealed, conductor=CONDUCTOR) == []
