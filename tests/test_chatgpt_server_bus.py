from __future__ import annotations

import importlib.util
import subprocess
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
WORKER = load("chatgpt_server_worker", "chatgpt_server_worker.py")


@pytest.fixture()
def round_db(tmp_path: Path) -> Path:
    path = tmp_path / "ai-bus.sqlite3"
    BUS.create_round(
        path,
        round_id="round-1",
        sender="gemini",
        participants=["chatgpt", "codex", "claude"],
        question="Which invariant should the release gate enforce?",
    )
    return path


def test_each_actor_can_claim_only_its_addressed_task(round_db: Path) -> None:
    chatgpt = BUS.claim(round_db, recipient="chatgpt", worker_id="chatgpt-browser")
    codex = BUS.claim(round_db, recipient="codex", worker_id="codex-worker")
    assert chatgpt and chatgpt["to"] == "chatgpt"
    assert codex and codex["to"] == "codex"
    assert chatgpt["from"] == "gemini"
    assert chatgpt["type"] == "deliberate"
    assert chatgpt["refs"] == codex["refs"]
    assert "prompt" not in chatgpt
    assert BUS.claim(round_db, recipient="chatgpt", worker_id="other-worker") is None


def test_results_are_unreadable_until_every_participant_completes(round_db: Path) -> None:
    task = BUS.claim(round_db, recipient="chatgpt", worker_id="chatgpt-browser")
    assert task
    BUS.complete(
        round_db, task_id=task["task_id"], recipient="chatgpt",
        lease_token=task["lease_token"], result="independent GPT answer",
    )
    with pytest.raises(BUS.BusError, match="hidden"):
        BUS.bundle(round_db, round_id="round-1")
    assert "independent GPT answer" not in str(BUS.status(round_db, round_id="round-1"))


def test_round_seals_only_after_all_answers_are_complete(round_db: Path) -> None:
    for actor in ("chatgpt", "codex", "claude"):
        task = BUS.claim(round_db, recipient=actor, worker_id=f"{actor}-worker")
        assert task
        settled = BUS.complete(
            round_db, task_id=task["task_id"], recipient=actor,
            lease_token=task["lease_token"], result=f"{actor} answer",
        )
    assert settled["round_phase"] == "sealed"
    payload = BUS.bundle(round_db, round_id="round-1")
    assert [row["from"] for row in payload["answers"]] == ["chatgpt", "claude", "codex"]
    assert all(set(row) >= {"from", "type", "refs", "status"} for row in payload["answers"])
    first = payload["answers"][0]["refs"][0]
    assert BUS.sealed_artifact(round_db, round_id="round-1", ref=first)["body"] == "chatgpt answer"


def test_artifact_text_is_stored_once_and_messages_use_refs(round_db: Path) -> None:
    with BUS.connect(round_db) as db:
        assert db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1
        rows = db.execute("SELECT input_refs_json FROM tasks").fetchall()
    assert len({row[0] for row in rows}) == 1


def test_failed_browser_delivery_never_returns_to_pending(round_db: Path) -> None:
    task = BUS.claim(round_db, recipient="chatgpt", worker_id="chatgpt-browser")
    assert task
    result = BUS.attention(
        round_db, task_id=task["task_id"], recipient="chatgpt",
        lease_token=task["lease_token"], error="socket hang up after possible submission",
    )
    assert result["automatic_retry"] is False
    assert BUS.claim(round_db, recipient="chatgpt", worker_id="chatgpt-browser") is None


def test_wrong_lease_cannot_complete_another_seat(round_db: Path) -> None:
    task = BUS.claim(round_db, recipient="chatgpt", worker_id="chatgpt-browser")
    assert task
    with pytest.raises(BUS.BusError) as failure:
        BUS.complete(
            round_db, task_id=task["task_id"], recipient="codex",
            lease_token=task["lease_token"], result="stolen",
        )
    assert failure.value.code == "LEASE_MISMATCH"


def test_worker_uses_a_throwaway_profile_and_records_the_answer(round_db: Path, tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    npx = tmp_path / "npx"
    npx.write_text("fixture", encoding="utf-8")
    seen: list[list[str]] = []

    def execute(argv, **kwargs):
        seen.append(list(argv))
        output = Path(argv[argv.index("--write-output") + 1])
        output.write_text("browser ChatGPT answer", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="done", stderr="")

    result = WORKER.run_one(
        db_path=round_db,
        recipient="chatgpt",
        worker_id="chatgpt-browser",
        profile=profile,
        state_dir=tmp_path / "runs",
        npx=npx,
        execute=execute,
    )
    assert result["status"] == "done"
    assert "--copy-profile" in seen[0]
    assert "--remote-chrome" not in seen[0]
    assert seen[0][seen[0].index("--model") + 1] == "gpt-5.6"


def test_worker_marks_nonzero_oracle_exit_for_attention_without_retry(round_db: Path, tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    npx = tmp_path / "npx"
    npx.write_text("fixture", encoding="utf-8")

    def execute(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="socket hang up")

    result = WORKER.run_one(
        db_path=round_db,
        recipient="chatgpt",
        worker_id="chatgpt-browser",
        profile=profile,
        state_dir=tmp_path / "runs",
        npx=npx,
        execute=execute,
    )
    assert result["status"] == "attention_required"
    assert BUS.claim(round_db, recipient="chatgpt", worker_id="chatgpt-browser") is None


def _seal(db_path: Path) -> dict:
    for actor in ("chatgpt", "codex", "claude"):
        task = BUS.claim(db_path, recipient=actor, worker_id=f"{actor}-worker")
        assert task
        BUS.complete(
            db_path, task_id=task["task_id"], recipient=actor,
            lease_token=task["lease_token"], result=f"{actor} answer",
        )
    return BUS.bundle(db_path, round_id="round-1")


def test_silence_never_counts_as_having_read_the_opposition(round_db: Path) -> None:
    sealed = _seal(round_db)
    BUS.acknowledge_bundle(
        round_db, round_id="round-1", participant="chatgpt",
        bundle_sha256=sealed["bundle_sha256"],
    )
    with pytest.raises(BUS.BusError) as failure:
        BUS.propose(round_db, round_id="round-1", sender="gemini", proposal="ship it")
    assert failure.value.code == "READ_RECEIPTS_PENDING"


def test_an_objection_blocks_the_proposal_and_only_its_owner_can_close_it(round_db: Path) -> None:
    sealed = _seal(round_db)
    for actor in ("chatgpt", "codex", "claude"):
        BUS.acknowledge_bundle(
            round_db, round_id="round-1", participant=actor,
            bundle_sha256=sealed["bundle_sha256"],
        )
    BUS.open_issue(
        round_db, round_id="round-1", participant="claude",
        issue_id="unsafe-release", summary="The rollback proof is missing.",
    )
    with pytest.raises(BUS.BusError) as wrong_owner:
        BUS.resolve_issue(
            round_db, round_id="round-1", participant="gemini",
            issue_id="unsafe-release", resolution="ignore it",
        )
    assert wrong_owner.value.code == "ISSUE_OWNER_REQUIRED"
    with pytest.raises(BUS.BusError) as blocked:
        BUS.propose(round_db, round_id="round-1", sender="gemini", proposal="ship it")
    assert blocked.value.code == "OPEN_ISSUES"
    BUS.resolve_issue(
        round_db, round_id="round-1", participant="claude",
        issue_id="unsafe-release", resolution="Rollback proof is now attached.",
    )
    for actor in ("chatgpt", "codex", "claude"):
        BUS.complete_review(round_db, round_id="round-1", participant=actor)
    assert BUS.propose(
        round_db, round_id="round-1", sender="gemini", proposal="ship after rollback check"
    )["status"] == "open"


def test_acknowledgement_alone_never_counts_as_completed_review(round_db: Path) -> None:
    sealed = _seal(round_db)
    for actor in ("chatgpt", "codex", "claude"):
        BUS.acknowledge_bundle(
            round_db, round_id="round-1", participant=actor,
            bundle_sha256=sealed["bundle_sha256"],
        )
    with pytest.raises(BUS.BusError) as pending:
        BUS.propose(round_db, round_id="round-1", sender="gemini", proposal="ship it")
    assert pending.value.code == "REVIEWS_PENDING"


def test_participant_cannot_object_after_marking_review_complete(round_db: Path) -> None:
    sealed = _seal(round_db)
    BUS.acknowledge_bundle(
        round_db, round_id="round-1", participant="claude",
        bundle_sha256=sealed["bundle_sha256"],
    )
    BUS.complete_review(round_db, round_id="round-1", participant="claude")
    with pytest.raises(BUS.BusError) as closed:
        BUS.open_issue(
            round_db, round_id="round-1", participant="claude",
            issue_id="late-objection", summary="This objection arrived too late.",
        )
    assert closed.value.code == "REVIEW_ALREADY_COMPLETE"


def test_consensus_requires_every_explicit_vote_on_the_same_proposal(round_db: Path) -> None:
    sealed = _seal(round_db)
    for actor in ("chatgpt", "codex", "claude"):
        BUS.acknowledge_bundle(
            round_db, round_id="round-1", participant=actor,
            bundle_sha256=sealed["bundle_sha256"],
        )
        BUS.complete_review(round_db, round_id="round-1", participant=actor)
    proposal = BUS.propose(
        round_db, round_id="round-1", sender="gemini", proposal="final release plan"
    )
    proposal_ref = proposal["refs"][0]
    BUS.vote(
        round_db, round_id="round-1", participant="chatgpt",
        proposal_ref=proposal_ref, decision="approve",
    )
    BUS.vote(
        round_db, round_id="round-1", participant="codex",
        proposal_ref=proposal_ref, decision="approve",
    )
    with pytest.raises(BUS.BusError) as missing:
        BUS.finalize(
            round_db, round_id="round-1", sender="gemini", proposal_ref=proposal_ref
        )
    assert missing.value.code == "UNANIMOUS_APPROVAL_REQUIRED"
    BUS.vote(
        round_db, round_id="round-1", participant="claude",
        proposal_ref=proposal_ref, decision="approve",
    )
    decision = BUS.finalize(
        round_db, round_id="round-1", sender="gemini", proposal_ref=proposal_ref
    )
    assert decision["status"] == "consensus"
    state = BUS.status(round_db, round_id="round-1")
    assert state["ui"]["event"] == "consensus"
    assert state["ui"]["seat_colors"]["chatgpt"] == "#10A37F"
