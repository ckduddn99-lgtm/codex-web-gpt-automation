#!/usr/bin/env python3
"""Walk a sealed round through its consensus barriers without judging anything.

The conductor is split in two on purpose. Gemini decides what to ask and writes the
final proposal from the sealed answers; those are judgement and they reach a model the
same way every seat's work does. This program owns the other half -- who has
acknowledged, whose review is closed, whether an objection is still open, how the votes
fall -- and it owns that half precisely because none of it is judgement. Every one of
those questions is a state check, so an LLM could only add a way to get it wrong.

That split is also what keeps the conductor from steering the outcome it authored, and
it removes the surface where a seat's answer could talk the conductor into finalizing:
this program reads seat text only through a fixed vocabulary and never as instruction.

Two rules the code exists to hold:

  Silence is not consent. A seat that did not answer, failed, timed out, or wrote
  something this parser does not recognise blocks its stage. Nothing is inferred from
  absence, and nothing is retried automatically -- the model may already have acted.

  One action per run. `advance` performs the single next thing and returns. It is safe
  to call repeatedly and expects to be, so a timer or the Gemini worker can drive it
  without the program holding any state of its own.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Callable


def _load_bus():
    spec = importlib.util.spec_from_file_location(
        "chatgpt_server_bus", Path(__file__).resolve().parent / "chatgpt_server_bus.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("chatgpt_server_bus", module)
    spec.loader.exec_module(module)
    return module


BUS = _load_bus()

STAGE_ACK = "ack"
STAGE_REVIEW = "review"
STAGE_RESOLVE = "resolve"
STAGE_PROPOSE = "propose"
STAGE_VOTE = "vote"


class DriverBlocked(RuntimeError):
    """A stage cannot advance and must not be advanced by guessing."""

    def __init__(self, stage: str, reason: str, detail: dict[str, Any]):
        super().__init__(reason)
        self.stage = stage
        self.reason = reason
        self.detail = detail


def final_line(body: str | None) -> str:
    """The seat's decision is the last non-empty line.

    Models reason before they answer, and a decision buried mid-paragraph would make
    the parser guess which sentence counted. Requiring the decision last is a rule the
    instruction states and this reads back literally.
    """
    for line in reversed((body or "").splitlines()):
        if line.strip():
            return line.strip()
    return ""


def parse_ack(body: str | None, *, digest: str) -> bool:
    """`ACK <sha256>`, and the digest has to be the one that was sealed.

    Echoing the hash is the whole point: it is the difference between a seat that read
    the bundle and a seat that said yes.
    """
    parts = final_line(body).split()
    return len(parts) == 2 and parts[0].upper() == "ACK" and parts[1].lower() == digest.lower()


def parse_review(body: str | None) -> tuple[str, str]:
    line = final_line(body)
    head, _, rest = line.partition(" ")
    head = head.upper().rstrip(":")
    if head == "REVIEW_COMPLETE":
        return "review_complete", ""
    if head == "OBJECT" and rest.strip():
        return "object", rest.strip()
    return "unparsed", line


def parse_resolution(body: str | None) -> tuple[str, str]:
    line = final_line(body)
    head, _, rest = line.partition(" ")
    head = head.upper().rstrip(":")
    if head == "RESOLVED" and rest.strip():
        return "resolved", rest.strip()
    if head == "STILL_OPEN":
        # A raiser who is not satisfied is a legitimate outcome, not a failure. The
        # round stays where it is until the objection is answered.
        return "still_open", rest.strip()
    return "unparsed", line


def parse_vote(body: str | None) -> tuple[str, str]:
    line = final_line(body)
    head, _, rest = line.partition(" ")
    head = head.upper().rstrip(":")
    if head in {"APPROVE", "ABSTAIN"}:
        return head.casefold(), rest.strip()
    if head == "REJECT" and rest.strip():
        return "reject", rest.strip()
    return "unparsed", line


def _answers(db: Path, round_id: str, conductor: str, stage: str) -> dict[str, dict[str, Any]]:
    payload = BUS.stage_results(db, round_id=round_id, sender=conductor, stage=stage)
    return {row["from"]: row for row in payload["answers"]}


def _harvest(
    db: Path, *, round_id: str, conductor: str, stage: str, expected: list[str],
    apply: Callable[[str, dict[str, Any]], bool],
) -> dict[str, Any]:
    """Turn a stage's finished answers into bus calls; stop on anything unclear.

    `apply` returns True when the answer moved the round forward. Seats that have not
    finished are simply awaited. Seats that failed or wrote something unrecognised are
    reported and block the stage -- this is where "no automatic retry" lives, because
    the work may already have happened on the provider's side.
    """
    answers = _answers(db, round_id, conductor, stage)
    waiting: list[str] = []
    blocked: list[dict[str, str]] = []
    applied: list[str] = []
    for participant in expected:
        row = answers.get(participant)
        if row is None or row["status"] in {"pending", "running"}:
            waiting.append(participant)
            continue
        if row["status"] != "completed":
            blocked.append({"participant": participant, "why": row["status"],
                            "detail": (row.get("error") or "")[:300]})
            continue
        try:
            if apply(participant, row):
                applied.append(participant)
        except DriverBlocked as stop:
            blocked.append({"participant": participant, "why": stop.reason,
                            "detail": json.dumps(stop.detail, ensure_ascii=False)[:300]})
    return {"stage": stage, "applied": applied, "waiting": waiting, "blocked": blocked}


def _ack_instruction(payload: dict[str, Any]) -> str:
    answers = "\n\n".join(
        f"### {row['from']}\n{row.get('body') or '(no body)'}" for row in payload["answers"]
    )
    return (
        "The round is sealed. Below are every seat's independent answers and the digest "
        "that binds them.\n\n"
        f"BUNDLE_SHA256: {payload['bundle_sha256']}\n\n"
        f"{answers}\n\n"
        "Read them. Then make the LAST LINE of your reply exactly:\n\n"
        f"    ACK {payload['bundle_sha256']}\n\n"
        "Echoing the digest is what separates having read the bundle from saying yes. "
        "If you cannot read it, say so instead -- do not write the line."
    )


REVIEW_INSTRUCTION = (
    "You have acknowledged the sealed bundle. Now register objections, if you have any.\n\n"
    "An objection is a reason this round must not proceed as it stands. Only you can "
    "close an objection you raise, so do not raise one you are not prepared to resolve. "
    "Agreeing is a real answer; a review that finds nothing is not a failed review.\n\n"
    "Make the LAST LINE of your reply exactly one of:\n\n"
    "    REVIEW_COMPLETE\n"
    "    OBJECT <one line saying what is wrong>\n\n"
    "You cannot add an objection after you close your review, so raise it now or not at all."
)

VOTE_INSTRUCTION = (
    "A final proposal has been published for this round. It is immutable and it is the "
    "exact text you are voting on.\n\n{proposal}\n\n"
    "Consensus requires every participant to approve. Missing, rejecting and abstaining "
    "votes all block it, and your vote cannot be changed once cast.\n\n"
    "Make the LAST LINE of your reply exactly one of:\n\n"
    "    APPROVE\n"
    "    REJECT <one line saying why>\n"
    "    ABSTAIN\n\n"
    "Approve only what you actually agree with. Blocking is a legitimate outcome."
)

PROPOSE_INSTRUCTION = (
    "Every seat has acknowledged the sealed bundle and closed its objection review with "
    "no objection left open. Write the final proposal for this round.\n\n"
    "It is immutable once published and every participant must approve it verbatim, so "
    "state the decision and its reasoning, and carry the disagreements that were not "
    "resolved rather than smoothing them away. Write the proposal itself as your whole "
    "reply -- no preamble, no closing marker.\n\n{bundle}"
)


def advance(db: Path, *, round_id: str, conductor: str = "gemini") -> dict[str, Any]:
    """Do the single next thing this round needs, then return what happened."""
    state = BUS.status(db, round_id=round_id)
    phase = state["phase"]
    participants: list[str] = state["participants"]
    base = {"schema": BUS.SCHEMA, "round_id": state["round_id"], "phase": phase}

    if phase == "consensus":
        return {**base, "action": "none", "done": True}

    if phase == "collect":
        outstanding = [
            row["recipient"] for row in state["tasks"]
            if row["stage"] == "collect" and row["status"] != "completed"
        ]
        return {**base, "action": "await_collection", "waiting": outstanding}

    sealed = BUS.bundle(db, round_id=round_id)
    digest = sealed["bundle_sha256"]

    # 1. Everyone must prove they read the exact bundle.
    if state["read_receipts"]["received"] < state["read_receipts"]["required"]:
        BUS.stage_task(
            db, round_id=round_id, sender=conductor, stage=STAGE_ACK,
            recipients=participants, instruction=_ack_instruction(sealed), kind="acknowledge",
        )

        def _apply_ack(participant: str, row: dict[str, Any]) -> bool:
            if not parse_ack(row["body"], digest=digest):
                raise DriverBlocked(STAGE_ACK, "unrecognised_acknowledgement",
                                    {"final_line": final_line(row["body"])})
            BUS.acknowledge_bundle(db, round_id=round_id, participant=participant,
                                   bundle_sha256=digest)
            return True

        return {**base, "action": "collect_acknowledgements",
                **_harvest(db, round_id=round_id, conductor=conductor, stage=STAGE_ACK,
                           expected=participants, apply=_apply_ack)}

    # 2. An objection blocks everything until the seat that raised it closes it.
    if state["open_issues"]:
        open_rows = BUS.status(db, round_id=round_id)
        raisers = _open_issue_owners(db, round_id)
        BUS.stage_task(
            db, round_id=round_id, sender=conductor, stage=STAGE_RESOLVE,
            recipients=sorted({owner for owner, _ in raisers}),
            instruction=(
                "You raised an objection on this round. Only you can close it.\n\n"
                "Make the LAST LINE of your reply exactly one of:\n\n"
                "    RESOLVED <one line saying what settled it>\n"
                "    STILL_OPEN\n\n"
                "STILL_OPEN is a real answer. Do not close an objection you do not "
                "consider answered."
            ),
            kind="resolve",
        )

        def _apply_resolution(participant: str, row: dict[str, Any]) -> bool:
            verdict, text = parse_resolution(row["body"])
            if verdict == "unparsed":
                raise DriverBlocked(STAGE_RESOLVE, "unrecognised_resolution",
                                    {"final_line": text})
            if verdict == "still_open":
                return False
            for owner, issue_id in raisers:
                if owner == participant:
                    BUS.resolve_issue(db, round_id=round_id, participant=participant,
                                      issue_id=issue_id, resolution=text)
            return True

        result = _harvest(db, round_id=round_id, conductor=conductor, stage=STAGE_RESOLVE,
                          expected=sorted({owner for owner, _ in raisers}),
                          apply=_apply_resolution)
        return {**base, "action": "resolve_objections",
                "open_issues": open_rows["open_issues"], **result}

    # 3. Every seat states explicitly that it is done raising objections.
    if state["reviews"]["received"] < state["reviews"]["required"]:
        BUS.stage_task(
            db, round_id=round_id, sender=conductor, stage=STAGE_REVIEW,
            recipients=participants, instruction=REVIEW_INSTRUCTION, kind="review",
        )

        def _apply_review(participant: str, row: dict[str, Any]) -> bool:
            verdict, text = parse_review(row["body"])
            if verdict == "unparsed":
                raise DriverBlocked(STAGE_REVIEW, "unrecognised_review", {"final_line": text})
            if verdict == "object":
                BUS.open_issue(db, round_id=round_id, participant=participant,
                               issue_id=f"{participant}-1", summary=text)
                return True
            BUS.complete_review(db, round_id=round_id, participant=participant)
            return True

        return {**base, "action": "collect_reviews",
                **_harvest(db, round_id=round_id, conductor=conductor, stage=STAGE_REVIEW,
                           expected=participants, apply=_apply_review)}

    # 4. The conductor writes the proposal. This is the judgement half.
    proposal_ref = _proposal_ref(db, round_id)
    if proposal_ref is None:
        BUS.stage_task(
            db, round_id=round_id, sender=conductor, stage=STAGE_PROPOSE,
            recipients=[conductor],
            instruction=PROPOSE_INSTRUCTION.format(bundle=_ack_instruction(sealed)),
            kind="propose",
        )
        drafted = _answers(db, round_id, conductor, STAGE_PROPOSE).get(conductor)
        if drafted is None or drafted["status"] in {"pending", "running"}:
            return {**base, "action": "await_proposal", "waiting": [conductor]}
        if drafted["status"] != "completed" or not (drafted["body"] or "").strip():
            return {**base, "action": "await_proposal", "waiting": [],
                    "blocked": [{"participant": conductor, "why": drafted["status"],
                                 "detail": (drafted.get("error") or "")[:300]}]}
        published = BUS.propose(db, round_id=round_id, sender=conductor,
                                proposal=drafted["body"])
        return {**base, "action": "publish_proposal", "refs": published["refs"]}

    # 5. Explicit unanimous approval, or no consensus.
    if state["votes_received"] < len(participants):
        proposal_body = BUS.sealed_artifact(db, round_id=round_id, ref=proposal_ref)["body"]
        BUS.stage_task(
            db, round_id=round_id, sender=conductor, stage=STAGE_VOTE,
            recipients=participants,
            instruction=VOTE_INSTRUCTION.format(proposal=proposal_body), kind="vote",
        )

        def _apply_vote(participant: str, row: dict[str, Any]) -> bool:
            decision, rationale = parse_vote(row["body"])
            if decision == "unparsed":
                raise DriverBlocked(STAGE_VOTE, "unrecognised_vote", {"final_line": rationale})
            BUS.vote(db, round_id=round_id, participant=participant,
                     proposal_ref=proposal_ref, decision=decision, rationale=rationale or None)
            return True

        return {**base, "action": "collect_votes",
                **_harvest(db, round_id=round_id, conductor=conductor, stage=STAGE_VOTE,
                           expected=participants, apply=_apply_vote)}

    # 6. finalize() re-checks every barrier and refuses anything short of unanimity.
    try:
        decided = BUS.finalize(db, round_id=round_id, sender=conductor,
                               proposal_ref=proposal_ref)
    except BUS.BusError as refused:
        return {**base, "action": "no_consensus", "code": refused.code, "detail": str(refused)}
    return {**base, "action": "finalized", "phase": "consensus",
            "decided_at": decided["decided_at"]}


def _proposal_ref(db: Path, round_id: str) -> int | None:
    with BUS.connect(db) as handle:
        row = handle.execute(
            "SELECT proposal_ref FROM rounds WHERE id = ?", (round_id,)
        ).fetchone()
    return int(row["proposal_ref"]) if row and row["proposal_ref"] is not None else None


def _open_issue_owners(db: Path, round_id: str) -> list[tuple[str, str]]:
    with BUS.connect(db) as handle:
        rows = handle.execute(
            "SELECT opened_by, issue_id FROM issues WHERE round_id = ? AND status = 'open'"
            " ORDER BY issue_id",
            (round_id,),
        ).fetchall()
    return [(row["opened_by"], row["issue_id"]) for row in rows]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    step = commands.add_parser("advance", help="perform the single next consensus action")
    step.add_argument("--round-id", required=True)
    step.add_argument("--conductor", default="gemini")
    return parser


def main(argv: list[str] | None = None, *, output: Callable[[str], None] = print) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = advance(args.db, round_id=args.round_id, conductor=args.conductor)
    except BUS.BusError as exc:
        output(json.dumps({"schema": BUS.SCHEMA, "status": "attention_required",
                           "code": exc.code, "error": str(exc)}, ensure_ascii=False))
        return 2
    output(json.dumps(payload, ensure_ascii=False))
    return 2 if payload.get("blocked") else 0


if __name__ == "__main__":
    raise SystemExit(main())
