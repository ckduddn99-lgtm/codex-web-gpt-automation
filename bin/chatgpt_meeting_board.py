#!/usr/bin/env python
"""Fan-in meeting board: many independent chat sessions, one room, server-held phases.

This replaces the "spawn N children inside one web Chat" contract. A real session was
driven against ordinary Chat on 2026-09-07 and it has no child-creation primitive at
all, so the unit of an agent here is one ordinary chat session. Sessions join a room
from outside; nothing is spawned.

The room has three phases and this module owns them. No phase depends on a client
promising not to look:

    collect -> sealed -> open

* collect  Participants submit and may read nothing but the question and their own
  receipt. A submission never passes through the shared room at all: `submit` writes
  the text straight into host-only state and leaves a hash receipt behind. There is
  no window in which one participant's answer sits somewhere another participant was
  told to read.
* sealed   Once every registered participant has submitted, `seal` orders the
  submissions by participant id, serializes them as canonical bytes, records the
  SHA-256 of exactly those bytes, and only then publishes the plaintext.
* open     Replies are accepted. Each carries author, reply_to, phase, issue_id,
  created_at and its own hash, so the reply relation survives - the thing N handoff
  files cannot express.

Cross-examination is not free discussion. An issue must be opened first, naming the
conflict and the participants it addresses, and a reply must land on an open issue.

Honest boundary: this is the file-backed prototype. Its guarantee is that the protocol
never writes phase-1 text where another participant is directed to look, and that every
read path refuses out of phase. It is not an OS trust boundary - a participant that runs
local processes can read host-only state directly. Closing that is exactly what promoting
this to a REST service on its own host buys, and it is why the gate has to exist from the
first commit rather than be retrofitted once transcripts already exist.

See docs/MEETING_BOARD_V0.md.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SCHEMA_ROOM = "codex.chatgpt.meeting-board-room/v1"
SCHEMA_RECEIPT = "codex.chatgpt.meeting-board-receipt/v1"
SCHEMA_SUBMISSION = "codex.chatgpt.meeting-board-submission/v1"
SCHEMA_BUNDLE = "codex.chatgpt.meeting-board-bundle/v1"
SCHEMA_ISSUE = "codex.chatgpt.meeting-board-issue/v1"
SCHEMA_REPLY = "codex.chatgpt.meeting-board-reply/v1"

ROOM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")
PARTICIPANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
ISSUE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,47}$")
NUMBERED_RE = re.compile(r"^(\d{8})\.json$")

PHASE_COLLECT = "collect"
PHASE_OPEN = "open"
PHASES = (PHASE_COLLECT, PHASE_OPEN)

MAX_TEXT_BYTES = 64 * 1024
MAX_WAIT_SECONDS = 300.0
MIN_PARTICIPANTS = 2
MAX_PARTICIPANTS = 8


class MeetingBoardError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_bytes(value: Any) -> bytes:
    """Exactly the bytes a hash commits to: sorted keys, no incidental whitespace."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _write_private(path: Path, data: bytes) -> None:
    _atomic_write(path, data)
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def _safe_room_id(value: str | None = None) -> str:
    room_id = (value or f"board-{secrets.token_hex(6)}").strip().casefold()
    if not ROOM_ID_RE.fullmatch(room_id):
        raise MeetingBoardError("ROOM_ID_INVALID", "room id must be 8-64 lowercase letters, digits, or hyphens")
    return room_id


def _safe_participant_id(value: str) -> str:
    participant = (value or "").strip().casefold()
    if not PARTICIPANT_ID_RE.fullmatch(participant):
        raise MeetingBoardError("PARTICIPANT_ID_INVALID", "participant id must be 2-32 lowercase word characters")
    return participant


def _safe_issue_id(value: str) -> str:
    issue = (value or "").strip().casefold()
    if not ISSUE_ID_RE.fullmatch(issue):
        raise MeetingBoardError("ISSUE_ID_INVALID", "issue id must be 2-48 lowercase word characters")
    return issue


def _validated_text(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise MeetingBoardError("TEXT_INVALID", "text must be a nonempty string")
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise MeetingBoardError("TEXT_TOO_LARGE", f"text exceeds {MAX_TEXT_BYTES} UTF-8 bytes")
    return text


def host_state_root() -> Path:
    """Where phase-1 text lives: deliberately outside the project the agents work in."""
    return Path.home() / ".codex" / "state" / "meeting-board"


@dataclass(frozen=True)
class BoardRuntime:
    room_id: str
    root: Path
    private_root: Path

    @property
    def room_path(self) -> Path:
        return self.root / "room.json"

    @property
    def question_path(self) -> Path:
        return self.root / "question.md"

    @property
    def receipts_path(self) -> Path:
        return self.root / "receipts"

    @property
    def sealed_path(self) -> Path:
        return self.root / "sealed"

    @property
    def issues_path(self) -> Path:
        return self.root / "issues"

    @property
    def replies_path(self) -> Path:
        return self.root / "replies"

    @property
    def invites_path(self) -> Path:
        return self.private_root / "invites"

    @property
    def submissions_path(self) -> Path:
        return self.private_root / "submissions"


def _canonical_dir(path: Path) -> Path:
    root = path.expanduser().resolve(strict=True)
    if not root.is_dir() or root.parent == root:
        raise MeetingBoardError("PATH_INVALID", "path must be an existing non-drive-root directory")
    return root


def _read_room(root: Path) -> dict[str, Any]:
    try:
        metadata = json.loads((root / "room.json").read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MeetingBoardError("ROOM_MISSING", "room.json is not present") from exc
    if metadata.get("schema") != SCHEMA_ROOM:
        raise MeetingBoardError("ROOM_INVALID", "room metadata schema is invalid")
    if metadata.get("phase") not in PHASES:
        raise MeetingBoardError("ROOM_INVALID", "room phase is invalid")
    return metadata


def _write_room(runtime: BoardRuntime, metadata: dict[str, Any]) -> None:
    _atomic_write(runtime.room_path, canonical_bytes(metadata) + b"\n")


def open_room(room_root: Path, *, private_root: Path | None = None) -> BoardRuntime:
    root = _canonical_dir(room_root)
    metadata = _read_room(root)
    # The room records where its own host-only state lives. Without this a room created
    # anywhere but the default location cannot find its own invites, and every call from
    # the CLI would fail as INVITE_MISSING instead of as whatever it really was.
    if private_root is not None:
        private = Path(private_root)
    elif metadata.get("private_root"):
        private = Path(metadata["private_root"])
    else:
        private = host_state_root() / metadata["room_id"]
    return BoardRuntime(room_id=metadata["room_id"], root=root, private_root=private.expanduser())


def create_room(
    project_root: Path,
    *,
    participants: list[str],
    question: str,
    room_id: str | None = None,
    private_root: Path | None = None,
) -> tuple[BoardRuntime, dict[str, str]]:
    """Register the roster up front. A room with an open roster can never prove it sealed."""
    project = _canonical_dir(project_root)
    normalized_room = _safe_room_id(room_id)
    ordered: list[str] = []
    for raw in participants:
        participant = _safe_participant_id(raw)
        if participant in ordered:
            raise MeetingBoardError("PARTICIPANT_DUPLICATE", f"participant {participant} is registered twice")
        ordered.append(participant)
    if not MIN_PARTICIPANTS <= len(ordered) <= MAX_PARTICIPANTS:
        raise MeetingBoardError(
            "PARTICIPANT_COUNT_INVALID",
            f"a board needs {MIN_PARTICIPANTS}-{MAX_PARTICIPANTS} participants",
        )
    question_text = _validated_text(question)

    root = project / ".codex-tmp" / "meeting-board" / normalized_room
    root.mkdir(parents=True, exist_ok=False)
    private = (private_root or host_state_root() / normalized_room).expanduser()
    private.mkdir(parents=True, exist_ok=True)
    runtime = BoardRuntime(room_id=normalized_room, root=root, private_root=private)
    for directory in (
        runtime.receipts_path,
        runtime.issues_path,
        runtime.replies_path,
        runtime.invites_path,
        runtime.submissions_path,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    _atomic_write(runtime.question_path, question_text.encode("utf-8"))
    tokens: dict[str, str] = {}
    for participant in ordered:
        token = secrets.token_urlsafe(32)
        tokens[participant] = token
        # Only the digest is stored. A leaked room directory does not hand anyone a seat.
        _write_private(
            runtime.invites_path / f"{participant}.json",
            canonical_bytes({
                "participant_id": participant,
                "token_sha256": _sha256(token.encode("ascii")),
                "created_at": _utc_now(),
            }) + b"\n",
        )
    _write_room(runtime, {
        "schema": SCHEMA_ROOM,
        "room_id": normalized_room,
        "project_root": str(project),
        "private_root": str(private.resolve()),
        "phase": PHASE_COLLECT,
        "participants": ordered,
        "question_sha256": _sha256(question_text.encode("utf-8")),
        "created_at": _utc_now(),
        "sealed_at": None,
        "bundle_sha256": None,
    })
    return runtime, tokens


def read_token_file(path: Path) -> str:
    """Missions get a path, not a secret.

    A mission's bytes are hashed into the run receipt and quoted back in state, so a
    token embedded in one would outlive the room in places nobody thinks to redact.
    """
    token = path.expanduser().resolve(strict=True).read_text(encoding="ascii").strip()
    if len(token) < 32 or "\n" in token or "\r" in token:
        raise MeetingBoardError("TOKEN_FILE_INVALID", "token file does not hold exactly one token")
    return token


def _resolve_token(token: str | None, token_file: Path | None) -> str:
    if (token is None) == (token_file is None):
        raise MeetingBoardError("TOKEN_INVALID", "pass exactly one of --token or --token-file")
    return token if token is not None else read_token_file(token_file)


def _authenticate(runtime: BoardRuntime, participant: str, token: str) -> str:
    participant_id = _safe_participant_id(participant)
    metadata = _read_room(runtime.root)
    if participant_id not in metadata["participants"]:
        raise MeetingBoardError("PARTICIPANT_UNKNOWN", "participant is not registered in this room")
    invite_path = runtime.invites_path / f"{participant_id}.json"
    try:
        invite = json.loads(invite_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MeetingBoardError("INVITE_MISSING", "no invite exists for this participant") from exc
    if not isinstance(token, str) or not token.strip():
        raise MeetingBoardError("TOKEN_INVALID", "token must be a nonempty string")
    if not secrets.compare_digest(invite["token_sha256"], _sha256(token.strip().encode("ascii"))):
        raise MeetingBoardError("TOKEN_MISMATCH", "token does not match this participant's invite")
    return participant_id


def _numbered(path: Path) -> list[tuple[int, Path]]:
    if not path.is_dir():
        return []
    found = []
    for candidate in path.iterdir():
        match = NUMBERED_RE.fullmatch(candidate.name)
        if match and candidate.is_file():
            found.append((int(match.group(1)), candidate))
    return sorted(found)


def submit(runtime: BoardRuntime, *, participant: str, token: str, text: str) -> dict[str, Any]:
    """Phase 1. The text goes to host-only state; the room only ever gets a hash receipt."""
    participant_id = _authenticate(runtime, participant, token)
    metadata = _read_room(runtime.root)
    if metadata["phase"] != PHASE_COLLECT:
        raise MeetingBoardError("PHASE_CLOSED", "submissions are only accepted while the room is collecting")
    body = _validated_text(text)
    receipt_path = runtime.receipts_path / f"{participant_id}.json"
    if receipt_path.exists():
        raise MeetingBoardError("SUBMISSION_DUPLICATE", "this participant has already submitted")
    digest = _sha256(body.encode("utf-8"))
    created_at = _utc_now()
    _write_private(
        runtime.submissions_path / f"{participant_id}.json",
        canonical_bytes({
            "schema": SCHEMA_SUBMISSION,
            "participant_id": participant_id,
            "text": body,
            "text_sha256": digest,
            "created_at": created_at,
        }) + b"\n",
    )
    receipt = {
        "schema": SCHEMA_RECEIPT,
        "participant_id": participant_id,
        "text_sha256": digest,
        "text_bytes": len(body.encode("utf-8")),
        "created_at": created_at,
    }
    _atomic_write(receipt_path, canonical_bytes(receipt) + b"\n")
    return receipt


def submitted_participants(runtime: BoardRuntime) -> list[str]:
    return sorted(path.stem for path in runtime.receipts_path.glob("*.json"))


def status(runtime: BoardRuntime) -> dict[str, Any]:
    metadata = _read_room(runtime.root)
    submitted = submitted_participants(runtime)
    return {
        "ok": True,
        "room_id": metadata["room_id"],
        "phase": metadata["phase"],
        "participants": metadata["participants"],
        "submitted": submitted,
        "pending": [p for p in metadata["participants"] if p not in submitted],
        "withdrawn": metadata.get("withdrawn", []),
        "bundle_sha256": metadata.get("bundle_sha256"),
        "issues": sorted(path.stem for path in runtime.issues_path.glob("*.json")),
        "reply_count": len(_numbered(runtime.replies_path)),
    }


def withdraw(runtime: BoardRuntime, *, participant: str, reason: str) -> dict[str, Any]:
    """Drop a seat that never arrived, so one dead session cannot deadlock the room.

    Sealing requires every registered participant, which is what makes "all answers are
    in" mean anything - but it also means a session that dies before submitting freezes
    the board forever. The recovery is not to seal partially and call it whole: it is to
    change the roster on the record, so a later reader of a three-answer bundle can see
    that three was not the plan.

    Only while collecting, and only a participant that has not submitted. A seat that has
    already answered stays in - removing it would be editing the collected set.
    """
    participant_id = _safe_participant_id(participant)
    metadata = _read_room(runtime.root)
    if metadata["phase"] != PHASE_COLLECT:
        raise MeetingBoardError("PHASE_CLOSED", "a participant can only be withdrawn while collecting")
    if participant_id not in metadata["participants"]:
        raise MeetingBoardError("PARTICIPANT_UNKNOWN", "participant is not registered in this room")
    if (runtime.receipts_path / f"{participant_id}.json").exists():
        raise MeetingBoardError(
            "SUBMISSION_ALREADY_IN",
            "this participant has already submitted; withdrawing it would edit the collected set",
        )
    remaining = [p for p in metadata["participants"] if p != participant_id]
    if len(remaining) < MIN_PARTICIPANTS:
        raise MeetingBoardError(
            "PARTICIPANT_COUNT_INVALID",
            f"withdrawing would leave fewer than {MIN_PARTICIPANTS} participants",
        )
    record = {"participant_id": participant_id, "reason": _validated_text(reason), "at": _utc_now()}
    metadata["participants"] = remaining
    metadata["withdrawn"] = [*metadata.get("withdrawn", []), record]
    _write_room(runtime, metadata)
    # The seat is gone, so its invite must be too - otherwise a session that comes back
    # late could still submit into a roster it is no longer part of.
    with contextlib.suppress(FileNotFoundError):
        (runtime.invites_path / f"{participant_id}.json").unlink()
    return {"ok": True, "withdrawn": record, "participants": remaining}


def seal(runtime: BoardRuntime) -> dict[str, Any]:
    """Every registered participant must have submitted. Partial sealing is refused."""
    metadata = _read_room(runtime.root)
    if metadata["phase"] != PHASE_COLLECT:
        raise MeetingBoardError("PHASE_CLOSED", "only a collecting room can be sealed")
    missing = [p for p in metadata["participants"] if not (runtime.receipts_path / f"{p}.json").exists()]
    if missing:
        raise MeetingBoardError(
            "SUBMISSIONS_INCOMPLETE",
            "the room cannot seal until every participant has submitted",
            {"pending": missing},
        )
    entries = []
    for participant in sorted(metadata["participants"]):
        stored = json.loads((runtime.submissions_path / f"{participant}.json").read_text(encoding="utf-8"))
        receipt = json.loads((runtime.receipts_path / f"{participant}.json").read_text(encoding="utf-8"))
        # Re-hash the stored text rather than trusting the digest stored next to it:
        # anything that could rewrite the text could rewrite that field in the same
        # edit. The receipt is the one side that was published during phase 1, so it
        # is the side worth comparing against.
        if _sha256(stored["text"].encode("utf-8")) != receipt["text_sha256"]:
            raise MeetingBoardError(
                "SUBMISSION_TAMPERED",
                "stored submission does not match the receipt published at submit time",
                {"participant_id": participant},
            )
        entries.append({
            "participant_id": participant,
            "text": stored["text"],
            "text_sha256": stored["text_sha256"],
            "created_at": stored["created_at"],
        })
    bundle = {
        "schema": SCHEMA_BUNDLE,
        "room_id": metadata["room_id"],
        "question_sha256": metadata["question_sha256"],
        # Carried into the sealed bytes so a short bundle can never be read as a full
        # room. Whoever reads three answers sees that a fourth seat was dropped and why.
        "withdrawn": metadata.get("withdrawn", []),
        "entries": entries,
    }
    payload = canonical_bytes(bundle)
    bundle_sha256 = _sha256(payload)
    runtime.sealed_path.mkdir(parents=True, exist_ok=True)
    _atomic_write(runtime.sealed_path / "bundle.json", payload + b"\n")
    _atomic_write(runtime.sealed_path / "bundle.sha256", (bundle_sha256 + "\n").encode("ascii"))
    metadata["phase"] = PHASE_OPEN
    metadata["sealed_at"] = _utc_now()
    metadata["bundle_sha256"] = bundle_sha256
    _write_room(runtime, metadata)
    return {"ok": True, "phase": PHASE_OPEN, "bundle_sha256": bundle_sha256, "entries": len(entries)}


def read_bundle(runtime: BoardRuntime, *, participant: str, token: str) -> dict[str, Any]:
    """Phase 2. Refused before the seal - this is the read half of the gate."""
    _authenticate(runtime, participant, token)
    metadata = _read_room(runtime.root)
    if metadata["phase"] != PHASE_OPEN:
        raise MeetingBoardError(
            "PHASE_NOT_OPEN",
            "other participants' answers are not readable until every answer is sealed",
        )
    payload = (runtime.sealed_path / "bundle.json").read_bytes().rstrip(b"\n")
    if _sha256(payload) != metadata["bundle_sha256"]:
        raise MeetingBoardError("BUNDLE_TAMPERED", "sealed bundle does not match the recorded hash")
    return json.loads(payload.decode("utf-8"))


def open_issue(runtime: BoardRuntime, *, issue_id: str, summary: str, participants: list[str]) -> dict[str, Any]:
    """Cross-examination is scoped: an issue names the conflict and who has to answer it."""
    metadata = _read_room(runtime.root)
    if metadata["phase"] != PHASE_OPEN:
        raise MeetingBoardError("PHASE_NOT_OPEN", "issues can only be opened after the seal")
    normalized = _safe_issue_id(issue_id)
    target = runtime.issues_path / f"{normalized}.json"
    if target.exists():
        raise MeetingBoardError("ISSUE_DUPLICATE", "this issue id already exists")
    addressed = [_safe_participant_id(p) for p in participants]
    unknown = [p for p in addressed if p not in metadata["participants"]]
    if unknown:
        raise MeetingBoardError(
            "PARTICIPANT_UNKNOWN",
            "issue addresses a participant that is not in the room",
            {"unknown": unknown},
        )
    if len(set(addressed)) < 2:
        raise MeetingBoardError("ISSUE_NOT_A_CONFLICT", "an issue must address at least two participants")
    issue = {
        "schema": SCHEMA_ISSUE,
        "issue_id": normalized,
        "summary": _validated_text(summary),
        "participants": sorted(set(addressed)),
        "created_at": _utc_now(),
    }
    _atomic_write(target, canonical_bytes(issue) + b"\n")
    return issue


def reply(
    runtime: BoardRuntime,
    *,
    participant: str,
    token: str,
    issue_id: str,
    text: str,
    reply_to: int | None = None,
) -> dict[str, Any]:
    """Phase 2. Author, target, issue and hash all stay on the record."""
    participant_id = _authenticate(runtime, participant, token)
    metadata = _read_room(runtime.root)
    if metadata["phase"] != PHASE_OPEN:
        raise MeetingBoardError("PHASE_NOT_OPEN", "replies are only accepted after the seal")
    normalized_issue = _safe_issue_id(issue_id)
    issue_path = runtime.issues_path / f"{normalized_issue}.json"
    if not issue_path.exists():
        raise MeetingBoardError("ISSUE_UNKNOWN", "replies must land on an issue that was opened")
    issue = json.loads(issue_path.read_text(encoding="utf-8"))
    if participant_id not in issue["participants"]:
        raise MeetingBoardError("ISSUE_NOT_ADDRESSED", "this participant was not named on the issue")
    body = _validated_text(text)
    existing = _numbered(runtime.replies_path)
    if reply_to is not None:
        known = {message_id for message_id, _ in existing}
        if reply_to not in known:
            raise MeetingBoardError("REPLY_TO_UNKNOWN", "reply_to does not name an existing reply")
    message_id = existing[-1][0] + 1 if existing else 1
    record = {
        "schema": SCHEMA_REPLY,
        "id": message_id,
        "author": participant_id,
        "reply_to": reply_to,
        "phase": PHASE_OPEN,
        "issue_id": normalized_issue,
        "text": body,
        "text_sha256": _sha256(body.encode("utf-8")),
        "created_at": _utc_now(),
    }
    _atomic_write(runtime.replies_path / f"{message_id:08d}.json", canonical_bytes(record) + b"\n")
    return record


def watch(
    runtime: BoardRuntime,
    *,
    participant: str,
    token: str,
    after: int,
    wait_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Long-poll. While collecting it can only report the shape of the room, never a rival answer."""
    _authenticate(runtime, participant, token)
    wait_seconds = max(0.0, min(float(wait_seconds), MAX_WAIT_SECONDS))
    deadline = monotonic() + wait_seconds
    while True:
        metadata = _read_room(runtime.root)
        # Every response carries the phase, so a participant always learns the room
        # opened without needing a separate notification message that a cursor would
        # then have to skip past.
        envelope = {"ok": True, "phase": metadata["phase"], "bundle_sha256": metadata["bundle_sha256"]}
        if metadata["phase"] == PHASE_COLLECT:
            if monotonic() >= deadline:
                # No payload by construction: while collecting there is nothing a
                # participant may receive except who the room is still waiting on.
                return {**envelope, "status": "collecting", "cursor": after, "message": None,
                        "pending": status(runtime)["pending"]}
        else:
            available = [
                (message_id, path)
                for message_id, path in _numbered(runtime.replies_path)
                if message_id > after
            ]
            if available:
                message_id, path = available[0]
                record = json.loads(path.read_text(encoding="utf-8"))
                if record.get("schema") != SCHEMA_REPLY or record.get("id") != message_id:
                    raise MeetingBoardError("REPLY_INVALID", "reply schema or id is invalid")
                return {**envelope, "status": "reply", "cursor": message_id, "message": record}
            if monotonic() >= deadline:
                return {**envelope, "status": "open", "cursor": after, "message": None}
        sleep(min(0.2, max(0.0, deadline - monotonic())))


def build_attach_prompt(
    runtime: BoardRuntime,
    *,
    participant: str,
    token: str,
    python_executable: str,
    token_file: Path | None = None,
) -> str:
    """When a token file is given the prompt references it instead of the secret.

    A prompt that becomes a mission has its bytes hashed into the run receipt and quoted
    back in run state, so a literal token in one outlives the room in places nobody
    thinks to redact. Pasting a token by hand into a chat window is a different risk and
    stays supported.
    """
    script = str(Path(__file__).resolve())
    question = runtime.question_path.read_text(encoding="utf-8").strip()
    credential = f"--token-file {token_file}" if token_file is not None else f"--token {token}"
    fence = "```"
    return f"""# Meeting board - participant `{participant}`

You are one independent participant in a review. Other sessions are answering the same
question right now and you cannot see them. That is deliberate: what you are worth here
is an answer that was not anchored on theirs.

Stay in ordinary Chat mode. Do not change this session's model or reasoning UI. Do not
try to create sub-agents - ordinary Chat has no such primitive, and claiming one would
be a false receipt.

Run every board command through Remote Desktop Commander, exactly as written. `-X utf8`
is required: without it Windows stdin turns non-ASCII into surrogates and the process
dies on UnicodeEncodeError.

{fence}
{python_executable} -X utf8 {script} submit --room-root {runtime.root} --participant {participant} {credential} --text-file <your answer file>
{fence}

Then wait. `watch` returns as soon as the room opens:

{fence}
{python_executable} -X utf8 {script} watch --room-root {runtime.root} --participant {participant} {credential} --after 0 --wait 55
{fence}

Until every participant has submitted, `watch` reports the phase and nothing else. There
is no command that shows you another answer early, and its absence is not a transport
problem to route around.

After the seal, `read-bundle` gives you every answer at once, and `reply` lets you
cross-examine - but only on an issue that was opened, and only if you were named on it.

## The question

{question}
"""


def _print(payload: Any, output: Callable[[str], None] = print) -> None:
    output(json.dumps(payload, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fan-in meeting board for independent chat sessions.")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="register the roster and open the collect phase")
    create.add_argument("--project-root", type=Path, required=True)
    create.add_argument("--participants", required=True, help="comma-separated participant ids")
    create.add_argument("--question-file", type=Path, required=True)
    create.add_argument("--room-id")

    invite = commands.add_parser("invite", help="print one participant's attach prompt")
    invite.add_argument("--room-root", type=Path, required=True)
    invite.add_argument("--participant", required=True)
    invite.add_argument("--token")
    invite.add_argument("--token-file", type=Path)

    submit_parser = commands.add_parser("submit", help="submit this participant's independent answer")
    submit_parser.add_argument("--room-root", type=Path, required=True)
    submit_parser.add_argument("--participant", required=True)
    submit_parser.add_argument("--token")
    submit_parser.add_argument("--token-file", type=Path)
    submit_parser.add_argument("--text-file", type=Path, required=True)

    watch_parser = commands.add_parser("watch", help="long-poll for the seal and for replies")
    watch_parser.add_argument("--room-root", type=Path, required=True)
    watch_parser.add_argument("--participant", required=True)
    watch_parser.add_argument("--token")
    watch_parser.add_argument("--token-file", type=Path)
    watch_parser.add_argument("--after", type=int, default=0)
    watch_parser.add_argument("--wait", type=float, default=55.0)

    status_parser = commands.add_parser("status", help="show phase, roster and who is still pending")
    status_parser.add_argument("--room-root", type=Path, required=True)

    withdraw_parser = commands.add_parser("withdraw", help="drop a seat that never arrived")
    withdraw_parser.add_argument("--room-root", type=Path, required=True)
    withdraw_parser.add_argument("--participant", required=True)
    withdraw_parser.add_argument("--reason", required=True)

    seal_parser = commands.add_parser("seal", help="seal every answer and open the room")
    seal_parser.add_argument("--room-root", type=Path, required=True)

    bundle_parser = commands.add_parser("read-bundle", help="read every sealed answer")
    bundle_parser.add_argument("--room-root", type=Path, required=True)
    bundle_parser.add_argument("--participant", required=True)
    bundle_parser.add_argument("--token")
    bundle_parser.add_argument("--token-file", type=Path)

    issue_parser = commands.add_parser("open-issue", help="open one named conflict for cross-examination")
    issue_parser.add_argument("--room-root", type=Path, required=True)
    issue_parser.add_argument("--issue-id", required=True)
    issue_parser.add_argument("--summary", required=True)
    issue_parser.add_argument("--participants", required=True)

    reply_parser = commands.add_parser("reply", help="cross-examine on an open issue")
    reply_parser.add_argument("--room-root", type=Path, required=True)
    reply_parser.add_argument("--participant", required=True)
    reply_parser.add_argument("--token")
    reply_parser.add_argument("--token-file", type=Path)
    reply_parser.add_argument("--issue-id", required=True)
    reply_parser.add_argument("--text-file", type=Path, required=True)
    reply_parser.add_argument("--reply-to", type=int)

    return parser


def main(argv: list[str] | None = None, *, output: Callable[[str], None] = print) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "create":
            runtime, tokens = create_room(
                args.project_root,
                participants=[p for p in args.participants.split(",") if p.strip()],
                question=args.question_file.read_text(encoding="utf-8"),
                room_id=args.room_id,
            )
            _print(
                {
                    "ok": True,
                    "room_id": runtime.room_id,
                    "room_root": str(runtime.root),
                    "private_root": str(runtime.private_root),
                    "tokens": tokens,
                },
                output,
            )
            return 0
        runtime = open_room(args.room_root)
        # Operator commands (status, seal, withdraw, open-issue) carry no seat identity,
        # so only the participant-facing ones resolve a token.
        token = ""
        if args.command in {"invite", "submit", "watch", "read-bundle", "reply"}:
            token = _resolve_token(getattr(args, "token", None), getattr(args, "token_file", None))
        if args.command == "invite":
            _authenticate(runtime, args.participant, token)
            output(
                build_attach_prompt(
                    runtime,
                    participant=_safe_participant_id(args.participant),
                    token=token,
                    token_file=args.token_file,
                    python_executable=sys.executable,
                )
            )
        elif args.command == "submit":
            _print(
                submit(
                    runtime,
                    participant=args.participant,
                    token=token,
                    text=args.text_file.read_text(encoding="utf-8"),
                ),
                output,
            )
        elif args.command == "watch":
            _print(
                watch(
                    runtime,
                    participant=args.participant,
                    token=token,
                    after=args.after,
                    wait_seconds=args.wait,
                ),
                output,
            )
        elif args.command == "status":
            _print(status(runtime), output)
        elif args.command == "withdraw":
            _print(withdraw(runtime, participant=args.participant, reason=args.reason), output)
        elif args.command == "seal":
            _print(seal(runtime), output)
        elif args.command == "read-bundle":
            _print(read_bundle(runtime, participant=args.participant, token=token), output)
        elif args.command == "open-issue":
            _print(
                open_issue(
                    runtime,
                    issue_id=args.issue_id,
                    summary=args.summary,
                    participants=[p for p in args.participants.split(",") if p.strip()],
                ),
                output,
            )
        elif args.command == "reply":
            _print(
                reply(
                    runtime,
                    participant=args.participant,
                    token=token,
                    issue_id=args.issue_id,
                    text=args.text_file.read_text(encoding="utf-8"),
                    reply_to=args.reply_to,
                ),
                output,
            )
        return 0
    except MeetingBoardError as exc:
        _print({"ok": False, "code": exc.code, "error": str(exc), "evidence": exc.evidence}, output)
        return 2


def _force_utf8_stdio() -> None:
    """Windows defaults these to the console codepage, which corrupts the JSON we print.

    The live canary already found the stdin half of this (Korean turned into surrogates
    and the process died on UnicodeEncodeError). The stdout half is quieter and worse:
    `create` prints room paths and invite tokens, and under cp949 a caller redirecting
    that to a file gets bytes it cannot decode back. Process-global state belongs to the
    process entry point, so it is set here and not on import.
    """
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(encoding="utf-8")


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    _force_utf8_stdio()
    raise SystemExit(main())
