#!/usr/bin/env python3
"""Small SQLite meeting bus with sealed-round visibility and lease-safe delivery.

The database is transport state, not a shared transcript. Message rows carry compact
numeric refs; immutable text is stored once in the artifact table. A participant can
claim only work addressed to it, and result refs stay hidden until every participant
has completed the round. Failed browser deliveries are never put back in the queue
automatically because the prompt may already have reached ChatGPT.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence


SCHEMA = "codex.chatgpt.server-bus/v1"
ACTOR_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
ROUND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_TEXT_BYTES = 1_000_000
SEAT_COLORS = {
    "gemini": "#4285F4",
    "chatgpt": "#10A37F",
    "codex": "#F59E0B",
    "claude": "#D97757",
}


class BusError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@contextmanager
def provider_slot(lock_path: Path) -> Iterator[bool]:
    """Try to reserve the single heavyweight-provider slot for this host.

    The advisory lock is released by the OS if a worker exits or crashes.  Yielding
    ``False`` lets a polling worker back off without claiming a bus task.
    """
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    acquired = False
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            if path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError:
                pass
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
        yield acquired
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _actor(value: str) -> str:
    value = (value or "").strip().casefold()
    if not ACTOR_RE.fullmatch(value):
        raise BusError("ACTOR_INVALID", "actor must be 2-32 lowercase letters, digits, '_' or '-'")
    return value


def _round_id(value: str) -> str:
    value = (value or "").strip()
    if not ROUND_RE.fullmatch(value):
        raise BusError("ROUND_INVALID", "round id must be 1-64 safe ASCII characters")
    return value


def _text(value: str, *, field: str) -> str:
    value = value if isinstance(value, str) else ""
    if not value.strip():
        raise BusError("TEXT_REQUIRED", f"{field} must not be empty")
    if len(value.encode("utf-8")) > MAX_TEXT_BYTES:
        raise BusError("TEXT_TOO_LARGE", f"{field} exceeds {MAX_TEXT_BYTES} bytes")
    return value


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 30000")
    db.execute("PRAGMA journal_mode = WAL")
    return db


def initialize(path: Path) -> None:
    with connect(path) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS artifacts (
                ref INTEGER PRIMARY KEY AUTOINCREMENT,
                sha256 TEXT NOT NULL UNIQUE,
                body TEXT NOT NULL,
                media_type TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rounds (
                id TEXT PRIMARY KEY,
                sender TEXT NOT NULL,
                question_ref INTEGER NOT NULL REFERENCES artifacts(ref),
                participants_json TEXT NOT NULL,
                phase TEXT NOT NULL CHECK (phase IN ('collect', 'sealed', 'consensus')),
                created_at TEXT NOT NULL,
                sealed_at TEXT,
                bundle_sha256 TEXT,
                proposal_ref INTEGER REFERENCES artifacts(ref),
                decided_at TEXT
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_id TEXT NOT NULL REFERENCES rounds(id),
                sender TEXT NOT NULL,
                recipient TEXT NOT NULL,
                kind TEXT NOT NULL,
                -- A round is not one question: after the collection barrier seals it,
                -- the same seats still have to acknowledge the bundle, close their
                -- objection review, and vote.  Each of those is one action per seat,
                -- so uniqueness is per stage rather than per round.  'collect' is the
                -- original question stage and is the only one the bundle hash covers.
                stage TEXT NOT NULL DEFAULT 'collect',
                input_refs_json TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 100,
                status TEXT NOT NULL CHECK (
                    status IN ('pending', 'running', 'completed', 'attention_required')
                ),
                worker_id TEXT,
                lease_sha256 TEXT,
                claimed_at TEXT,
                completed_at TEXT,
                result_refs_json TEXT,
                error TEXT,
                UNIQUE(round_id, recipient, stage)
            );
            CREATE INDEX IF NOT EXISTS tasks_recipient_queue
                ON tasks(recipient, status, priority, id);
            CREATE TABLE IF NOT EXISTS read_receipts (
                round_id TEXT NOT NULL REFERENCES rounds(id),
                participant TEXT NOT NULL,
                bundle_sha256 TEXT NOT NULL,
                read_at TEXT NOT NULL,
                PRIMARY KEY (round_id, participant)
            );
            CREATE TABLE IF NOT EXISTS issues (
                round_id TEXT NOT NULL REFERENCES rounds(id),
                issue_id TEXT NOT NULL,
                opened_by TEXT NOT NULL,
                summary_ref INTEGER NOT NULL REFERENCES artifacts(ref),
                status TEXT NOT NULL CHECK (status IN ('open', 'resolved')),
                resolution_ref INTEGER REFERENCES artifacts(ref),
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                PRIMARY KEY (round_id, issue_id)
            );
            CREATE TABLE IF NOT EXISTS review_receipts (
                round_id TEXT NOT NULL REFERENCES rounds(id),
                participant TEXT NOT NULL,
                reviewed_at TEXT NOT NULL,
                PRIMARY KEY (round_id, participant)
            );
            CREATE TABLE IF NOT EXISTS votes (
                round_id TEXT NOT NULL REFERENCES rounds(id),
                participant TEXT NOT NULL,
                proposal_ref INTEGER NOT NULL REFERENCES artifacts(ref),
                vote TEXT NOT NULL CHECK (vote IN ('approve', 'reject', 'abstain')),
                rationale_ref INTEGER REFERENCES artifacts(ref),
                voted_at TEXT NOT NULL,
                PRIMARY KEY (round_id, participant)
            );
            """
        )
        _migrate_tasks_stage(db)


def _migrate_tasks_stage(db: sqlite3.Connection) -> None:
    """Give an already-created tasks table the stage column and per-stage uniqueness.

    CREATE TABLE IF NOT EXISTS silently skips a live database, so the schema above
    only describes new installs.  Every existing row predates staging and is by
    definition a collection answer, which is exactly what the DEFAULT says.

    SQLite cannot alter a UNIQUE constraint in place, so the table is rebuilt.  That
    is safe here and it is the cheap moment to do it: the constraint being replaced,
    UNIQUE(round_id, recipient), is what makes post-seal stages impossible.
    """
    columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
    if not columns or "stage" in columns:
        return
    db.executescript(
        """
        PRAGMA foreign_keys = OFF;
        BEGIN IMMEDIATE;
        CREATE TABLE tasks_staged (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id TEXT NOT NULL REFERENCES rounds(id),
            sender TEXT NOT NULL,
            recipient TEXT NOT NULL,
            kind TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT 'collect',
            input_refs_json TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 100,
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'running', 'completed', 'attention_required')
            ),
            worker_id TEXT,
            lease_sha256 TEXT,
            claimed_at TEXT,
            completed_at TEXT,
            result_refs_json TEXT,
            error TEXT,
            UNIQUE(round_id, recipient, stage)
        );
        INSERT INTO tasks_staged
            (id, round_id, sender, recipient, kind, stage, input_refs_json, priority,
             status, worker_id, lease_sha256, claimed_at, completed_at, result_refs_json, error)
        SELECT id, round_id, sender, recipient, kind, 'collect', input_refs_json, priority,
               status, worker_id, lease_sha256, claimed_at, completed_at, result_refs_json, error
        FROM tasks;
        DROP TABLE tasks;
        ALTER TABLE tasks_staged RENAME TO tasks;
        CREATE INDEX IF NOT EXISTS tasks_recipient_queue
            ON tasks(recipient, status, priority, id);
        COMMIT;
        PRAGMA foreign_keys = ON;
        """
    )


def _put_artifact(
    db: sqlite3.Connection,
    *,
    body: str,
    created_by: str,
    media_type: str = "text/markdown",
) -> int:
    body = _text(body, field="artifact")
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    db.execute(
        """INSERT OR IGNORE INTO artifacts
           (sha256, body, media_type, created_by, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (digest, body, media_type, _actor(created_by), _now()),
    )
    return int(db.execute("SELECT ref FROM artifacts WHERE sha256 = ?", (digest,)).fetchone()[0])


def create_round(
    path: Path,
    *,
    round_id: str,
    sender: str,
    participants: list[str],
    question: str,
    kind: str = "deliberate",
    priority: int = 100,
) -> dict[str, Any]:
    initialize(path)
    rid = _round_id(round_id)
    sender = _actor(sender)
    ordered: list[str] = []
    for raw in participants:
        participant = _actor(raw)
        if participant in ordered:
            raise BusError("PARTICIPANT_DUPLICATE", f"participant {participant} occurs twice")
        ordered.append(participant)
    if len(ordered) < 2:
        raise BusError("PARTICIPANTS_REQUIRED", "a sealed round needs at least two participants")
    question = _text(question, field="question")
    kind = _actor(kind)
    created = _now()
    try:
        with connect(path) as db:
            db.execute("BEGIN IMMEDIATE")
            question_ref = _put_artifact(db, body=question, created_by=sender)
            db.execute(
                """INSERT INTO rounds
                   (id, sender, question_ref, participants_json, phase, created_at)
                   VALUES (?, ?, ?, ?, 'collect', ?)""",
                (rid, sender, question_ref, json.dumps(ordered), created),
            )
            for participant in ordered:
                db.execute(
                    """INSERT INTO tasks
                       (round_id, sender, recipient, kind, input_refs_json, priority, status)
                       VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
                    (rid, sender, participant, kind, json.dumps([question_ref]), int(priority)),
                )
            db.execute("COMMIT")
    except sqlite3.IntegrityError as exc:
        raise BusError("ROUND_EXISTS", f"round {rid} already exists") from exc
    return {
        "schema": SCHEMA,
        "round_id": rid,
        "phase": "collect",
        "participants": ordered,
        "refs": [question_ref],
    }


def stage_task(
    path: Path, *, round_id: str, sender: str, stage: str, recipients: Sequence[str],
    instruction: str, kind: str = "stage", priority: int = 50,
) -> dict[str, Any]:
    """Address one post-seal stage to specific seats, once each.

    The workers are stage-agnostic: they claim whatever is addressed to them and run
    the model on it. So a consensus stage becomes real work exactly the same way the
    question did, which keeps the provider lock, the lease, and the
    attention-required path identical rather than growing a second execution route.

    Creation is idempotent because a driver is expected to run repeatedly and must not
    depend on being called exactly once. Seats that already hold this stage are
    skipped, not duplicated and not reset.
    """
    stage = _actor(stage)
    if stage == "collect":
        raise BusError("STAGE_RESERVED", "the collection stage is created by create-round")
    sender = _actor(sender)
    instruction = _text(instruction, field="instruction")
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM rounds WHERE id = ?", (_round_id(round_id),)).fetchone()
        if row is None:
            raise BusError("ROUND_UNKNOWN", "round does not exist")
        if row["phase"] != "sealed":
            # Before the seal the seats are still answering independently; a stage task
            # would hand them work that assumes they can see each other.
            raise BusError("ROUND_NOT_SEALED", "stages run after the collection barrier")
        if row["sender"] != sender:
            raise BusError("CONDUCTOR_REQUIRED", "only the round sender drives its stages")
        participants = json.loads(row["participants_json"])
        # The sender is addressable too, and not as a courtesy: writing the final
        # proposal from the sealed answers is the conductor's own judgement work, and
        # it reaches a model the same way every other seat's work does. Counting the
        # votes on that proposal stays mechanical, which is what keeps the conductor
        # from being able to steer the outcome it authored.
        addressable = set(participants) | {row["sender"]}
        wanted = [_actor(name) for name in recipients] or list(participants)
        unknown = [name for name in wanted if name not in addressable]
        if unknown:
            raise BusError("PARTICIPANT_UNKNOWN", f"not in this round: {', '.join(unknown)}")
        existing = {
            existing_row["recipient"]
            for existing_row in db.execute(
                "SELECT recipient FROM tasks WHERE round_id = ? AND stage = ?",
                (row["id"], stage),
            )
        }
        created: list[str] = []
        instruction_ref: int | None = None
        for participant in wanted:
            if participant in existing:
                continue
            if instruction_ref is None:
                # One artifact for the whole stage: the seats are being asked the same
                # thing, and storing the body once is the point of refs.
                instruction_ref = _put_artifact(db, body=instruction, created_by=sender)
            db.execute(
                """INSERT INTO tasks
                   (round_id, sender, recipient, kind, stage, input_refs_json, priority, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (row["id"], sender, participant, kind, stage,
                 json.dumps([instruction_ref]), int(priority)),
            )
            created.append(participant)
        db.execute("COMMIT")
    return {
        "schema": SCHEMA, "round_id": row["id"], "stage": stage,
        "created": created, "already_present": sorted(existing & set(wanted)),
        "refs": [instruction_ref] if instruction_ref is not None else [],
    }


def stage_results(path: Path, *, round_id: str, sender: str, stage: str) -> dict[str, Any]:
    """The conductor's view of one stage's answers.

    Seats read each other only through the sealed bundle, and sealed_artifact
    deliberately refuses stage refs so that nobody can resolve another seat's
    acknowledgement or vote. The driver still has to read those answers to turn them
    into bus calls, so this is that path and it is restricted to the round's sender.

    Unfinished seats are reported as themselves rather than omitted: a stage that is
    waiting on somebody looks different from a stage where somebody answered nothing,
    and the caller must never collapse the two.
    """
    stage = _actor(stage)
    sender = _actor(sender)
    with connect(path) as db:
        row = db.execute("SELECT * FROM rounds WHERE id = ?", (_round_id(round_id),)).fetchone()
        if row is None:
            raise BusError("ROUND_UNKNOWN", "round does not exist")
        if row["sender"] != sender:
            raise BusError("CONDUCTOR_REQUIRED", "only the round sender may read stage answers")
        rows = db.execute(
            """SELECT t.recipient, t.status, t.error, a.body
               FROM tasks AS t
               LEFT JOIN artifacts AS a
                 ON a.ref = CAST(json_extract(t.result_refs_json, '$[0]') AS INTEGER)
               WHERE t.round_id = ? AND t.stage = ?
               ORDER BY t.recipient""",
            (row["id"], stage),
        ).fetchall()
    return {
        "schema": SCHEMA,
        "round_id": row["id"],
        "stage": stage,
        "participants": json.loads(row["participants_json"]),
        "answers": [
            {
                "from": result["recipient"],
                "status": result["status"],
                "body": result["body"] if result["status"] == "completed" else None,
                "error": result["error"],
            }
            for result in rows
        ],
    }


def claim(path: Path, *, recipient: str, worker_id: str) -> dict[str, Any] | None:
    """Atomically lease one task. Running tasks never become pending implicitly."""
    initialize(path)
    recipient = _actor(recipient)
    worker_id = _actor(worker_id)
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            """SELECT id, round_id, sender, recipient, kind, stage, input_refs_json, priority
               FROM tasks WHERE recipient = ? AND status = 'pending'
               ORDER BY priority ASC, id ASC LIMIT 1""",
            (recipient,),
        ).fetchone()
        if row is None:
            db.execute("COMMIT")
            return None
        changed = db.execute(
            """UPDATE tasks SET status = 'running', worker_id = ?, lease_sha256 = ?,
               claimed_at = ? WHERE id = ? AND status = 'pending'""",
            (worker_id, digest, _now(), row["id"]),
        ).rowcount
        if changed != 1:  # pragma: no cover - BEGIN IMMEDIATE makes this defensive
            db.execute("ROLLBACK")
            raise BusError("CLAIM_RACE", "task changed while it was being claimed")
        db.execute("COMMIT")
    return {
        "schema": SCHEMA,
        "task_id": int(row["id"]),
        "round_id": row["round_id"],
        "from": row["sender"],
        "to": row["recipient"],
        "type": row["kind"],
        # Which barrier this task belongs to. A seat answering the question and a seat
        # acknowledging the sealed bundle are different acts with different valid
        # replies, and neither the worker nor the driver should have to infer that.
        "stage": row["stage"],
        "refs": json.loads(row["input_refs_json"]),
        "priority": int(row["priority"]),
        "status": "running",
        "lease_token": token,
    }


def _owned_running(db: sqlite3.Connection, task_id: int, recipient: str, token: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
    if row is None:
        raise BusError("TASK_UNKNOWN", "task does not exist")
    digest = hashlib.sha256((token or "").encode("ascii", "strict")).hexdigest()
    if row["recipient"] != _actor(recipient) or not secrets.compare_digest(row["lease_sha256"] or "", digest):
        raise BusError("LEASE_MISMATCH", "task lease does not belong to this recipient")
    if row["status"] != "running":
        raise BusError("TASK_NOT_RUNNING", "only a running task can be settled")
    return row


def task_inputs(path: Path, *, task_id: int, recipient: str, lease_token: str) -> list[dict[str, Any]]:
    """Resolve only refs attached to the caller's currently leased task."""
    with connect(path) as db:
        row = _owned_running(db, task_id, recipient, lease_token)
        refs = json.loads(row["input_refs_json"])
        artifacts: list[dict[str, Any]] = []
        for ref in refs:
            artifact = db.execute(
                "SELECT ref, sha256, body, media_type FROM artifacts WHERE ref = ?",
                (int(ref),),
            ).fetchone()
            if artifact is None:
                raise BusError("REF_UNKNOWN", f"artifact ref {ref} does not exist")
            artifacts.append(dict(artifact))
    return artifacts


def _bundle_descriptor(db: sqlite3.Connection, round_id: str) -> tuple[list[dict[str, Any]], str]:
    # Only the collection stage. The bundle hash is published at seal time and every
    # later reader re-derives it to detect tampering, so a task added afterwards -- an
    # acknowledgement, a review, a vote -- would silently change the digest and make
    # the round permanently unreadable with BUNDLE_TAMPERED.
    rows = db.execute(
        "SELECT recipient, result_refs_json FROM tasks"
        " WHERE round_id = ? AND stage = 'collect' ORDER BY recipient",
        (round_id,),
    ).fetchall()
    answers = [
        {"from": row["recipient"], "refs": json.loads(row["result_refs_json"] or "[]")}
        for row in rows
    ]
    encoded = json.dumps(answers, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return answers, hashlib.sha256(encoded).hexdigest()


def complete(path: Path, *, task_id: int, recipient: str, lease_token: str, result: str) -> dict[str, Any]:
    result = _text(result, field="result")
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = _owned_running(db, task_id, recipient, lease_token)
        finished = _now()
        result_ref = _put_artifact(db, body=result, created_by=recipient)
        db.execute(
            """UPDATE tasks SET status = 'completed', completed_at = ?, result_refs_json = ?,
               lease_sha256 = NULL WHERE id = ?""",
            (finished, json.dumps([result_ref]), int(task_id)),
        )
        # Sealing is the collection barrier, so only collection tasks hold it open.
        # A pending stage task belongs to a round that is already sealed.
        pending = db.execute(
            "SELECT COUNT(*) FROM tasks"
            " WHERE round_id = ? AND stage = 'collect' AND status != 'completed'",
            (row["round_id"],),
        ).fetchone()[0]
        if pending == 0:
            _, bundle_sha256 = _bundle_descriptor(db, row["round_id"])
            db.execute(
                """UPDATE rounds SET phase = 'sealed', sealed_at = ?, bundle_sha256 = ?
                   WHERE id = ? AND phase = 'collect'""",
                (finished, bundle_sha256, row["round_id"]),
            )
        phase = db.execute("SELECT phase FROM rounds WHERE id = ?", (row["round_id"],)).fetchone()[0]
        db.execute("COMMIT")
    return {
        "schema": SCHEMA,
        "task_id": int(task_id),
        "from": recipient,
        "to": row["sender"],
        "type": "result",
        "refs": [result_ref],
        "status": "done",
        "round_phase": phase,
    }


def attention(
    path: Path, *, task_id: int, recipient: str, lease_token: str, error: str
) -> dict[str, Any]:
    error = _text(error, field="error")
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = _owned_running(db, task_id, recipient, lease_token)
        db.execute(
            """UPDATE tasks SET status = 'attention_required', completed_at = ?, error = ?,
               lease_sha256 = NULL WHERE id = ?""",
            (_now(), error, int(task_id)),
        )
        db.execute("COMMIT")
    return {
        "schema": SCHEMA,
        "task_id": int(task_id),
        "round_id": row["round_id"],
        "status": "attention_required",
        "automatic_retry": False,
    }


def status(path: Path, *, round_id: str) -> dict[str, Any]:
    with connect(path) as db:
        round_row = db.execute("SELECT * FROM rounds WHERE id = ?", (_round_id(round_id),)).fetchone()
        if round_row is None:
            raise BusError("ROUND_UNKNOWN", "round does not exist")
        tasks = db.execute(
            "SELECT id, recipient, stage, status, priority, claimed_at, completed_at"
            " FROM tasks WHERE round_id = ? ORDER BY id",
            (round_row["id"],),
        ).fetchall()
        receipt_count = db.execute(
            "SELECT COUNT(*) FROM read_receipts WHERE round_id = ?", (round_row["id"],)
        ).fetchone()[0]
        open_issues = db.execute(
            "SELECT COUNT(*) FROM issues WHERE round_id = ? AND status = 'open'", (round_row["id"],)
        ).fetchone()[0]
        review_count = db.execute(
            "SELECT COUNT(*) FROM review_receipts WHERE round_id = ?", (round_row["id"],)
        ).fetchone()[0]
        vote_count = db.execute(
            "SELECT COUNT(*) FROM votes WHERE round_id = ?", (round_row["id"],)
        ).fetchone()[0]
    participants = json.loads(round_row["participants_json"])
    if round_row["phase"] == "consensus":
        event = "consensus"
    elif round_row["phase"] == "collect":
        event = "meeting_started"
    else:
        event = "moderating"
    return {
        "schema": SCHEMA,
        "round_id": round_row["id"],
        "phase": round_row["phase"],
        "participants": participants,
        "tasks": [dict(row) for row in tasks],
        "read_receipts": {"received": int(receipt_count), "required": len(participants)},
        "reviews": {"received": int(review_count), "required": len(participants)},
        "open_issues": int(open_issues),
        "votes_received": int(vote_count),
        "ui": {
            "event": event,
            "seat_colors": {actor: SEAT_COLORS.get(actor, "#6B7280") for actor in participants},
        },
    }


def bundle(path: Path, *, round_id: str) -> dict[str, Any]:
    """Reveal answers only after the all-participant barrier has sealed the round."""
    with connect(path) as db:
        round_row = db.execute("SELECT * FROM rounds WHERE id = ?", (_round_id(round_id),)).fetchone()
        if round_row is None:
            raise BusError("ROUND_UNKNOWN", "round does not exist")
        if round_row["phase"] not in {"sealed", "consensus"}:
            raise BusError("ROUND_NOT_SEALED", "participant results are hidden until every answer is complete")
        # Collection stage only, for the same reason _bundle_descriptor filters: the
        # bundle is the sealed set of answers to the question. A later stage's task
        # would both break the digest and put an acknowledgement or a vote into the
        # set of independent answers that the seats are about to read.
        rows = db.execute(
            "SELECT recipient, result_refs_json, completed_at FROM tasks"
            " WHERE round_id = ? AND stage = 'collect' ORDER BY recipient",
            (round_row["id"],),
        ).fetchall()
        question_ref = int(round_row["question_ref"])
        _, actual_sha256 = _bundle_descriptor(db, round_row["id"])
        if not secrets.compare_digest(round_row["bundle_sha256"] or "", actual_sha256):
            raise BusError("BUNDLE_TAMPERED", "sealed result refs do not match the recorded bundle hash")
    answers = [
        {
            "from": row["recipient"],
            "type": "result",
            "refs": json.loads(row["result_refs_json"]),
            "status": "done",
            "completed_at": row["completed_at"],
        }
        for row in rows
    ]
    return {
        "schema": SCHEMA,
        "round_id": round_row["id"],
        "phase": round_row["phase"],
        "bundle_sha256": actual_sha256,
        "refs": [question_ref],
        "answers": answers,
    }


def sealed_artifact(path: Path, *, round_id: str, ref: int) -> dict[str, Any]:
    """Resolve a ref only if it belongs to a sealed round."""
    with connect(path) as db:
        round_row = db.execute(
            "SELECT phase, question_ref, proposal_ref FROM rounds WHERE id = ?", (_round_id(round_id),)
        ).fetchone()
        if round_row is None:
            raise BusError("ROUND_UNKNOWN", "round does not exist")
        if round_row["phase"] != "sealed":
            raise BusError("ROUND_NOT_SEALED", "artifacts remain private until the round is sealed")
        allowed = {int(round_row["question_ref"])}
        # Collection results only. The proposal, the question and issue summaries are
        # allowed explicitly below, so narrowing this loses no legitimate read -- while
        # leaving it open would let a seat resolve another seat's acknowledgement or
        # vote text. Refs are small integers, so "nobody was told the number" is not a
        # control.
        for row in db.execute(
            "SELECT result_refs_json FROM tasks WHERE round_id = ? AND stage = 'collect'",
            (round_id,),
        ):
            allowed.update(int(value) for value in json.loads(row["result_refs_json"] or "[]"))
        if round_row["proposal_ref"] is not None:
            allowed.add(int(round_row["proposal_ref"]))
        for row in db.execute("SELECT summary_ref, resolution_ref FROM issues WHERE round_id = ?", (round_id,)):
            allowed.add(int(row["summary_ref"]))
            if row["resolution_ref"] is not None:
                allowed.add(int(row["resolution_ref"]))
        if int(ref) not in allowed:
            raise BusError("REF_NOT_IN_ROUND", "artifact ref is not part of this round")
        artifact = db.execute(
            "SELECT ref, sha256, body, media_type, created_by FROM artifacts WHERE ref = ?",
            (int(ref),),
        ).fetchone()
        if artifact is None:
            raise BusError("REF_UNKNOWN", "artifact ref does not exist")
    return dict(artifact)


def acknowledge_bundle(
    path: Path, *, round_id: str, participant: str, bundle_sha256: str
) -> dict[str, Any]:
    participant = _actor(participant)
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM rounds WHERE id = ?", (_round_id(round_id),)).fetchone()
        if row is None:
            raise BusError("ROUND_UNKNOWN", "round does not exist")
        if row["phase"] not in {"sealed", "consensus"}:
            raise BusError("ROUND_NOT_SEALED", "there is no complete bundle to acknowledge")
        if participant not in json.loads(row["participants_json"]):
            raise BusError("PARTICIPANT_UNKNOWN", "participant is not in this round")
        if not secrets.compare_digest(row["bundle_sha256"] or "", bundle_sha256 or ""):
            raise BusError("BUNDLE_HASH_MISMATCH", "read receipt does not name the sealed bundle")
        db.execute(
            """INSERT OR IGNORE INTO read_receipts
               (round_id, participant, bundle_sha256, read_at) VALUES (?, ?, ?, ?)""",
            (row["id"], participant, bundle_sha256, _now()),
        )
        db.execute("COMMIT")
    return {"round_id": round_id, "from": participant, "type": "read_receipt", "status": "done"}


def open_issue(
    path: Path, *, round_id: str, participant: str, issue_id: str, summary: str
) -> dict[str, Any]:
    participant = _actor(participant)
    issue_id = _round_id(issue_id)
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM rounds WHERE id = ?", (_round_id(round_id),)).fetchone()
        if row is None or row["phase"] != "sealed":
            raise BusError("ROUND_NOT_REVIEWABLE", "issues can be opened only on a sealed round")
        receipt = db.execute(
            "SELECT 1 FROM read_receipts WHERE round_id = ? AND participant = ?",
            (round_id, participant),
        ).fetchone()
        if receipt is None:
            raise BusError("READ_RECEIPT_REQUIRED", "participant must acknowledge the full bundle first")
        if row["proposal_ref"] is not None:
            raise BusError("REVIEW_CLOSED", "objections must be recorded before the final proposal")
        if db.execute(
            "SELECT 1 FROM review_receipts WHERE round_id = ? AND participant = ?",
            (round_id, participant),
        ).fetchone():
            raise BusError("REVIEW_ALREADY_COMPLETE", "participant already closed its review")
        summary_ref = _put_artifact(db, body=summary, created_by=participant)
        try:
            db.execute(
                """INSERT INTO issues
                   (round_id, issue_id, opened_by, summary_ref, status, created_at)
                   VALUES (?, ?, ?, ?, 'open', ?)""",
                (round_id, issue_id, participant, summary_ref, _now()),
            )
        except sqlite3.IntegrityError as exc:
            raise BusError("ISSUE_EXISTS", "issue id already exists in this round") from exc
        db.execute("COMMIT")
    return {"round_id": round_id, "from": participant, "type": "objection", "refs": [summary_ref], "issue_id": issue_id, "status": "open"}


def resolve_issue(
    path: Path, *, round_id: str, participant: str, issue_id: str, resolution: str
) -> dict[str, Any]:
    participant = _actor(participant)
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        issue = db.execute(
            "SELECT * FROM issues WHERE round_id = ? AND issue_id = ?",
            (_round_id(round_id), _round_id(issue_id)),
        ).fetchone()
        if issue is None:
            raise BusError("ISSUE_UNKNOWN", "issue does not exist")
        if issue["opened_by"] != participant:
            raise BusError("ISSUE_OWNER_REQUIRED", "only the objecting participant can close its issue")
        if issue["status"] != "open":
            raise BusError("ISSUE_ALREADY_RESOLVED", "issue is already resolved")
        resolution_ref = _put_artifact(db, body=resolution, created_by=participant)
        db.execute(
            """UPDATE issues SET status = 'resolved', resolution_ref = ?, resolved_at = ?
               WHERE round_id = ? AND issue_id = ?""",
            (resolution_ref, _now(), round_id, issue_id),
        )
        db.execute("COMMIT")
    return {"round_id": round_id, "from": participant, "type": "resolution", "refs": [resolution_ref], "issue_id": issue_id, "status": "done"}


def complete_review(path: Path, *, round_id: str, participant: str) -> dict[str, Any]:
    """Explicitly state that this participant has finished registering objections."""
    participant = _actor(participant)
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM rounds WHERE id = ?", (_round_id(round_id),)).fetchone()
        if row is None or row["phase"] != "sealed" or row["proposal_ref"] is not None:
            raise BusError("REVIEW_CLOSED", "review completion requires a sealed round without a proposal")
        if participant not in json.loads(row["participants_json"]):
            raise BusError("PARTICIPANT_UNKNOWN", "participant is not in this round")
        if db.execute(
            "SELECT 1 FROM read_receipts WHERE round_id = ? AND participant = ?",
            (round_id, participant),
        ).fetchone() is None:
            raise BusError("READ_RECEIPT_REQUIRED", "participant must acknowledge the bundle first")
        db.execute(
            """INSERT OR IGNORE INTO review_receipts (round_id, participant, reviewed_at)
               VALUES (?, ?, ?)""",
            (round_id, participant, _now()),
        )
        db.execute("COMMIT")
    return {"round_id": round_id, "from": participant, "type": "review_complete", "status": "done"}


def propose(path: Path, *, round_id: str, sender: str, proposal: str) -> dict[str, Any]:
    sender = _actor(sender)
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM rounds WHERE id = ?", (_round_id(round_id),)).fetchone()
        if row is None or row["phase"] != "sealed":
            raise BusError("ROUND_NOT_REVIEWABLE", "a proposal requires a sealed round")
        if row["sender"] != sender:
            raise BusError("CONDUCTOR_REQUIRED", "only the round sender can publish the final proposal")
        expected = len(json.loads(row["participants_json"]))
        receipts = db.execute("SELECT COUNT(*) FROM read_receipts WHERE round_id = ?", (round_id,)).fetchone()[0]
        if receipts != expected:
            raise BusError("READ_RECEIPTS_PENDING", "every participant must acknowledge the bundle")
        if db.execute("SELECT COUNT(*) FROM issues WHERE round_id = ? AND status = 'open'", (round_id,)).fetchone()[0]:
            raise BusError("OPEN_ISSUES", "the final proposal is blocked by unresolved objections")
        reviews = db.execute("SELECT COUNT(*) FROM review_receipts WHERE round_id = ?", (round_id,)).fetchone()[0]
        if reviews != expected:
            raise BusError("REVIEWS_PENDING", "every participant must explicitly finish its objection review")
        if row["proposal_ref"] is not None:
            raise BusError("PROPOSAL_EXISTS", "the round already has an immutable final proposal")
        proposal_ref = _put_artifact(db, body=proposal, created_by=sender)
        db.execute("UPDATE rounds SET proposal_ref = ? WHERE id = ?", (proposal_ref, round_id))
        db.execute("COMMIT")
    return {"round_id": round_id, "from": sender, "type": "proposal", "refs": [proposal_ref], "status": "open"}


def vote(
    path: Path, *, round_id: str, participant: str, proposal_ref: int,
    decision: str, rationale: str | None = None,
) -> dict[str, Any]:
    participant = _actor(participant)
    decision = (decision or "").strip().casefold()
    if decision not in {"approve", "reject", "abstain"}:
        raise BusError("VOTE_INVALID", "vote must be approve, reject, or abstain")
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM rounds WHERE id = ?", (_round_id(round_id),)).fetchone()
        if row is None or row["phase"] != "sealed" or int(row["proposal_ref"] or 0) != int(proposal_ref):
            raise BusError("PROPOSAL_MISMATCH", "vote must name this round's final proposal ref")
        if participant not in json.loads(row["participants_json"]):
            raise BusError("PARTICIPANT_UNKNOWN", "participant is not in this round")
        rationale_ref = None
        if rationale and rationale.strip():
            rationale_ref = _put_artifact(db, body=rationale, created_by=participant)
        try:
            db.execute(
                """INSERT INTO votes
                   (round_id, participant, proposal_ref, vote, rationale_ref, voted_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (round_id, participant, int(proposal_ref), decision, rationale_ref, _now()),
            )
        except sqlite3.IntegrityError as exc:
            raise BusError("VOTE_EXISTS", "a participant's final vote is immutable") from exc
        db.execute("COMMIT")
    return {"round_id": round_id, "from": participant, "type": "vote", "refs": [int(proposal_ref)], "vote": decision, "status": "done"}


def finalize(path: Path, *, round_id: str, sender: str, proposal_ref: int) -> dict[str, Any]:
    sender = _actor(sender)
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM rounds WHERE id = ?", (_round_id(round_id),)).fetchone()
        if row is None or row["phase"] != "sealed" or row["sender"] != sender:
            raise BusError("FINALIZE_NOT_ALLOWED", "only the sender can finalize a sealed round")
        participants = json.loads(row["participants_json"])
        if int(row["proposal_ref"] or 0) != int(proposal_ref):
            raise BusError("PROPOSAL_MISMATCH", "finalization must name the immutable proposal ref")
        if db.execute("SELECT COUNT(*) FROM issues WHERE round_id = ? AND status = 'open'", (round_id,)).fetchone()[0]:
            raise BusError("OPEN_ISSUES", "consensus is impossible while an objection remains open")
        receipts = db.execute("SELECT COUNT(*) FROM read_receipts WHERE round_id = ?", (round_id,)).fetchone()[0]
        if receipts != len(participants):
            raise BusError("READ_RECEIPTS_PENDING", "silence is not proof that the bundle was read")
        votes = db.execute(
            "SELECT participant, vote, proposal_ref FROM votes WHERE round_id = ?", (round_id,)
        ).fetchall()
        approved = {
            vote_row["participant"] for vote_row in votes
            if vote_row["vote"] == "approve" and int(vote_row["proposal_ref"]) == int(proposal_ref)
        }
        if approved != set(participants):
            raise BusError("UNANIMOUS_APPROVAL_REQUIRED", "missing, reject, and abstain votes never count as consent")
        decided_at = _now()
        db.execute("UPDATE rounds SET phase = 'consensus', decided_at = ? WHERE id = ?", (decided_at, round_id))
        db.execute("COMMIT")
    return {"round_id": round_id, "from": sender, "type": "decision", "refs": [int(proposal_ref)], "status": "consensus", "decided_at": decided_at}


def _read(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    create = commands.add_parser("create-round")
    create.add_argument("--round-id", required=True)
    create.add_argument("--sender", default="gemini")
    create.add_argument("--participants", required=True)
    create.add_argument("--question-file", type=Path, required=True)
    create.add_argument("--kind", default="deliberate")
    create.add_argument("--priority", type=int, default=100)
    stage = commands.add_parser("stage-task")
    stage.add_argument("--round-id", required=True)
    stage.add_argument("--sender", default="gemini")
    stage.add_argument("--stage", required=True)
    stage.add_argument("--recipients", default="", help="comma separated; empty means all participants")
    stage.add_argument("--instruction-file", type=Path, required=True)
    stage.add_argument("--kind", default="stage")
    stage.add_argument("--priority", type=int, default=50)
    results = commands.add_parser("stage-results")
    results.add_argument("--round-id", required=True)
    results.add_argument("--sender", default="gemini")
    results.add_argument("--stage", required=True)
    take = commands.add_parser("claim")
    take.add_argument("--recipient", required=True)
    take.add_argument("--worker-id", required=True)
    done = commands.add_parser("complete")
    done.add_argument("--task-id", type=int, required=True)
    done.add_argument("--recipient", required=True)
    done.add_argument("--lease-token", required=True)
    done.add_argument("--result-file", type=Path, required=True)
    failed = commands.add_parser("attention")
    failed.add_argument("--task-id", type=int, required=True)
    failed.add_argument("--recipient", required=True)
    failed.add_argument("--lease-token", required=True)
    failed.add_argument("--error", required=True)
    state = commands.add_parser("status")
    state.add_argument("--round-id", required=True)
    reveal = commands.add_parser("bundle")
    reveal.add_argument("--round-id", required=True)
    artifact = commands.add_parser("sealed-artifact")
    artifact.add_argument("--round-id", required=True)
    artifact.add_argument("--ref", type=int, required=True)
    receipt = commands.add_parser("ack")
    receipt.add_argument("--round-id", required=True)
    receipt.add_argument("--participant", required=True)
    receipt.add_argument("--bundle-sha256", required=True)
    issue = commands.add_parser("open-issue")
    issue.add_argument("--round-id", required=True)
    issue.add_argument("--participant", required=True)
    issue.add_argument("--issue-id", required=True)
    issue.add_argument("--summary-file", type=Path, required=True)
    resolution = commands.add_parser("resolve-issue")
    resolution.add_argument("--round-id", required=True)
    resolution.add_argument("--participant", required=True)
    resolution.add_argument("--issue-id", required=True)
    resolution.add_argument("--resolution-file", type=Path, required=True)
    reviewed = commands.add_parser("review-complete")
    reviewed.add_argument("--round-id", required=True)
    reviewed.add_argument("--participant", required=True)
    proposal = commands.add_parser("propose")
    proposal.add_argument("--round-id", required=True)
    proposal.add_argument("--sender", required=True)
    proposal.add_argument("--proposal-file", type=Path, required=True)
    ballot = commands.add_parser("vote")
    ballot.add_argument("--round-id", required=True)
    ballot.add_argument("--participant", required=True)
    ballot.add_argument("--proposal-ref", type=int, required=True)
    ballot.add_argument("--vote", required=True)
    ballot.add_argument("--rationale-file", type=Path)
    final = commands.add_parser("finalize")
    final.add_argument("--round-id", required=True)
    final.add_argument("--sender", required=True)
    final.add_argument("--proposal-ref", type=int, required=True)
    return parser


def main(argv: list[str] | None = None, *, output: Callable[[str], None] = print) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            initialize(args.db)
            payload: Any = {"schema": SCHEMA, "initialized": True, "db": str(args.db.resolve())}
        elif args.command == "create-round":
            payload = create_round(
                args.db,
                round_id=args.round_id,
                sender=args.sender,
                participants=args.participants.split(","),
                question=_read(args.question_file),
                kind=args.kind,
                priority=args.priority,
            )
        elif args.command == "stage-task":
            payload = stage_task(
                args.db,
                round_id=args.round_id,
                sender=args.sender,
                stage=args.stage,
                recipients=[name for name in args.recipients.split(",") if name.strip()],
                instruction=_read(args.instruction_file),
                kind=args.kind,
                priority=args.priority,
            )
        elif args.command == "stage-results":
            payload = stage_results(
                args.db, round_id=args.round_id, sender=args.sender, stage=args.stage
            )
        elif args.command == "claim":
            payload = claim(args.db, recipient=args.recipient, worker_id=args.worker_id)
            if payload is None:
                payload = {"schema": SCHEMA, "status": "idle"}
        elif args.command == "complete":
            payload = complete(
                args.db,
                task_id=args.task_id,
                recipient=args.recipient,
                lease_token=args.lease_token,
                result=_read(args.result_file),
            )
        elif args.command == "attention":
            payload = attention(
                args.db,
                task_id=args.task_id,
                recipient=args.recipient,
                lease_token=args.lease_token,
                error=args.error,
            )
        elif args.command == "status":
            payload = status(args.db, round_id=args.round_id)
        elif args.command == "bundle":
            payload = bundle(args.db, round_id=args.round_id)
        elif args.command == "sealed-artifact":
            payload = sealed_artifact(args.db, round_id=args.round_id, ref=args.ref)
        elif args.command == "ack":
            payload = acknowledge_bundle(
                args.db, round_id=args.round_id, participant=args.participant,
                bundle_sha256=args.bundle_sha256,
            )
        elif args.command == "open-issue":
            payload = open_issue(
                args.db, round_id=args.round_id, participant=args.participant,
                issue_id=args.issue_id, summary=_read(args.summary_file),
            )
        elif args.command == "resolve-issue":
            payload = resolve_issue(
                args.db, round_id=args.round_id, participant=args.participant,
                issue_id=args.issue_id, resolution=_read(args.resolution_file),
            )
        elif args.command == "review-complete":
            payload = complete_review(
                args.db, round_id=args.round_id, participant=args.participant,
            )
        elif args.command == "propose":
            payload = propose(
                args.db, round_id=args.round_id, sender=args.sender,
                proposal=_read(args.proposal_file),
            )
        elif args.command == "vote":
            payload = vote(
                args.db, round_id=args.round_id, participant=args.participant,
                proposal_ref=args.proposal_ref, decision=args.vote,
                rationale=_read(args.rationale_file) if args.rationale_file else None,
            )
        else:
            payload = finalize(
                args.db, round_id=args.round_id, sender=args.sender,
                proposal_ref=args.proposal_ref,
            )
        output(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except (BusError, OSError, sqlite3.Error) as exc:
        output(json.dumps({"ok": False, "code": getattr(exc, "code", "BUS_ERROR"), "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
