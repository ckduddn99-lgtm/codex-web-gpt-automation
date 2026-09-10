#!/usr/bin/env python3
"""Durable self-healing coordinator for attention-required goal task runs.

The coordinator never blindly replays an uncertain execution. It classifies the
failure, directly requeues only failures proven safe before execution, and uses
separate recovery tasks for partial/uncertain cases. Recovery tasks are linked to
the frozen original run and stay with ChatGPT; exhausted provider recovery becomes an
operator-repair boundary, never a synthetic user-decision requirement.
"""
from __future__ import annotations

import hashlib
import socket
import subprocess
import time
from pathlib import Path
from typing import Any
import sys

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import chatgpt_log_redaction as REDACTION
import chatgpt_server_bus as BUS

MAX_RECOVERY_ATTEMPTS = 3
RECOVERY_ASSIGNEE = "chatgpt"
RECOVERY_ASSIGNEES = ("chatgpt",) * MAX_RECOVERY_ATTEMPTS
BROWSER_HOST = "127.0.0.1"
BROWSER_PORT = 9222
ROOT_OPS_HELPER = "/usr/local/sbin/project-control-ops"


def _browser_port_open(host: str = BROWSER_HOST, port: int = BROWSER_PORT) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=0.5):
            return True
    except OSError:
        return False


def _browser_attach_refused(detail: str) -> bool:
    text = str(detail or "").casefold()
    return (
        "user error (browser-automation)" in text
        and f"connect econnrefused {BROWSER_HOST}:{BROWSER_PORT}" in text
    )


def _repair_browser_service() -> tuple[bool, str]:
    if _browser_port_open():
        return True, "browser CDP port already reachable"
    try:
        proc = subprocess.run(
            ["sudo", "-n", ROOT_OPS_HELPER, "start", "oracle-browser@board.service"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"restricted browser recovery could not run: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "no helper output").strip()[-1000:]
        return False, f"restricted browser recovery failed with exit {proc.returncode}: {detail}"
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if _browser_port_open():
            return True, "oracle-browser service started and CDP port is reachable"
        time.sleep(0.25)
    return False, "oracle-browser start returned success but CDP port 9222 never became reachable"


def _run_row(db_path: Path, run_id: int) -> dict[str, Any]:
    BUS.initialize(db_path)
    with BUS.connect(db_path) as db:
        row = db.execute(
            """SELECT r.*, t.repo_id, t.status AS task_status
               FROM goal_task_runs r
               JOIN goal_tasks t ON t.goal_id = r.goal_id AND t.task_id = r.task_id
               WHERE r.id = ?""",
            (int(run_id),),
        ).fetchone()
    if row is None:
        raise BUS.BusError("GOAL_TASK_RUN_UNKNOWN", "goal task run does not exist")
    return dict(row)


def _error_detail(db_path: Path, row: dict[str, Any]) -> str:
    """Read only the error artifact already bound to this exact goal-task run."""
    ref = row.get("error_ref")
    if ref is None:
        return ""
    with BUS.connect(db_path) as db:
        artifact = db.execute(
            "SELECT body FROM artifacts WHERE ref = ?",
            (int(ref),),
        ).fetchone()
    return str(artifact["body"]) if artifact is not None else ""


def classify_failure(*, error_code: str, assignee: str, detail: str) -> str:
    code = str(error_code or "").strip().upper()
    who = str(assignee or "").strip().casefold()
    text = detail.casefold()
    if code in {"MODEL_START_FAILED"}:
        return "pre_execution_safe"
    if code == "GOAL_REPO_ROUTE_INVALID":
        return "environment_recoverable"
    if code == "MODEL_DID_NOT_COMPLETE" and any(
        token in text for token in (
            "permission", "headless", "auto-denied", "no output produced",
            "connect econnrefused 127.0.0.1:9222",
        )
    ):
        return "environment_recoverable"
    if who == "gemini":
        # Gemini task execution is analysis-only and has no repository workspace.
        return "environment_recoverable"
    if code == "GOAL_TASK_RESPONSE_INVALID":
        return "partial_execution"
    return "uncertain_execution"


def _fingerprint(row: dict[str, Any], classification: str, detail: str) -> str:
    normalized = " ".join(REDACTION.redact_text(detail).casefold().split())[:1200]
    material = "\n".join((
        classification,
        str(row.get("error_code") or ""),
        str(row.get("assignee") or ""),
        str(row.get("repo_id") or ""),
        normalized,
    ))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _public_reason(row: dict[str, Any], classification: str) -> str:
    code = str(row.get("error_code") or "GOAL_TASK_RUN_ATTENTION")
    who = str(row.get("assignee") or "provider")
    if classification == "pre_execution_safe":
        return f"{who} failed before execution started ({code})."
    if classification == "environment_recoverable":
        return f"{who} hit a recoverable environment or routing failure ({code})."
    if classification == "partial_execution":
        return f"{who} may have executed work but returned an invalid result ({code})."
    return f"{who} execution state is uncertain ({code}); current state must be inspected first."


def _direct_requeue_is_safe(row: dict[str, Any], classification: str, detail: str) -> bool:
    if row.get("assignee") == "gemini":
        return True
    if classification == "pre_execution_safe" and row.get("assignee") != RECOVERY_ASSIGNEE:
        return True
    if (
        classification == "environment_recoverable"
        and row.get("assignee") == "gemini"
        and any(token in detail.casefold() for token in ("permission", "headless", "auto-denied"))
    ):
        return True
    return False


def _recovery_rows(db_path: Path, original_run_id: int) -> list[dict[str, Any]]:
    with BUS.connect(db_path) as db:
        rows = db.execute(
            """SELECT * FROM goal_task_recoveries
               WHERE original_run_id = ? ORDER BY attempt, id""",
            (int(original_run_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def _build_recovery_description(
    *, row: dict[str, Any], classification: str, fingerprint: str,
    attempt: int, detail: str,
) -> str:
    excerpt = REDACTION.redact_text(detail).strip()
    if len(excerpt) > 1800:
        excerpt = excerpt[-1800:]
    return (
        "Internal self-healing recovery task. Do not blindly repeat the original operation.\n"
        f"Original goal/task: {row['goal_id']}/{row['task_id']}\n"
        f"Original run: {row['id']}\n"
        f"Repository: {row['repo_id']}\n"
        f"Failure class: {classification}\n"
        f"Failure code: {row.get('error_code') or 'UNKNOWN'}\n"
        f"Recovery fingerprint: {fingerprint}\n"
        f"Attempt: {attempt}/{MAX_RECOVERY_ATTEMPTS}\n"
        "First inspect current repository/runtime state. If earlier work partially happened, preserve it, "
        "verify it, and finish only the missing safe portion. If the failure is environmental, repair the "
        "bounded project-specific cause. For project infrastructure repair, prefer Project Control registered "
        "break-glass host tools (project_ssh_status/project_ssh_exec); never invent a host and do not delegate "
        "repair to Codex or Claude. Never use reset/restore/branch switching/push or irreversible external "
        "actions. Return completed only after the cause is remediated and it is safe to resume the original "
        "task. Return user_decision_required only when a real user-only decision or irreversible external "
        "action is required.\n"
        f"Redacted diagnostic excerpt:\n{excerpt or '(no diagnostic body)'}\n"
    )


def _escalate_original(db_path: Path, row: dict[str, Any], *, reason: str) -> dict[str, Any]:
    if row["status"] == "attention_required":
        BUS.acknowledge_goal_task_run(
            db_path, run_id=int(row["id"]), changed_by="recovery",
            note=f"Automatic recovery exhausted. {reason}", requeue=False,
        )
    state = BUS.goal_status(db_path, goal_id=row["goal_id"])
    task = next(item for item in state["tasks"] if item["task_id"] == row["task_id"])
    if task["status"] == "in_progress":
        BUS.transition_goal_task(
            db_path, goal_id=row["goal_id"], task_id=row["task_id"],
            status="user_decision_required", changed_by="recovery",
            blocker=reason,
        )
    return {
        "action": "goal_task_recovery_escalated", "goal_id": row["goal_id"],
        "task_id": row["task_id"], "original_run_id": int(row["id"]),
        "reason": reason, "automatic_retry": False,
    }


def _defer_to_operator_repair(
    db_path: Path, row: dict[str, Any], *, classification: str,
) -> dict[str, Any]:
    reason = (
        f"Automatic provider recovery exhausted {MAX_RECOVERY_ATTEMPTS} ChatGPT attempts for "
        f"the same failure family ({classification}). Preserve evidence and wait for ChatGPT "
        "operator repair through Project Control break-glass tools; no user decision is implied."
    )
    if row["status"] == "attention_required":
        BUS.acknowledge_goal_task_run(
            db_path, run_id=int(row["id"]), changed_by="recovery",
            note=reason, requeue=False,
        )
    state = BUS.goal_status(db_path, goal_id=row["goal_id"])
    task = next(item for item in state["tasks"] if item["task_id"] == row["task_id"])
    if task["status"] == "in_progress":
        BUS.transition_goal_task(
            db_path, goal_id=row["goal_id"], task_id=row["task_id"],
            status="blocked", changed_by="recovery", blocker=reason,
            assignee=RECOVERY_ASSIGNEE,
        )
    return {
        "action": "goal_task_recovery_deferred", "goal_id": row["goal_id"],
        "task_id": row["task_id"], "original_run_id": int(row["id"]),
        "classification": classification, "assignee": RECOVERY_ASSIGNEE,
        "reason": reason, "automatic_retry": False,
    }


def recover_run(db_path: Path, *, run_id: int) -> dict[str, Any]:
    row = _run_row(db_path, run_id)
    if row["status"] != "attention_required":
        return {"action": "goal_task_recovery_not_needed", "run_id": int(run_id)}

    relation = BUS.goal_task_recovery_for_task(
        db_path, goal_id=row["goal_id"], task_id=row["task_id"]
    )
    if relation is not None:
        return _handle_failed_recovery_run(db_path, row=row, relation=relation)

    detail = _error_detail(db_path, row)
    classification = classify_failure(
        error_code=row.get("error_code") or "", assignee=row["assignee"], detail=detail
    )
    fingerprint = _fingerprint(row, classification, detail)
    previous = _recovery_rows(db_path, int(row["id"]))
    if not previous and _browser_attach_refused(detail):
        repaired, repair_detail = _repair_browser_service()
        if repaired:
            BUS.acknowledge_goal_task_run(
                db_path, run_id=int(row["id"]), changed_by="recovery",
                note=(
                    "Browser attach failed before prompt submission; restricted infrastructure recovery "
                    f"succeeded ({repair_detail}). Requeueing the original ChatGPT task safely."
                ),
                requeue=True, reassign_to=RECOVERY_ASSIGNEE,
            )
            return {
                "action": "goal_task_recovery_resumed", "goal_id": row["goal_id"],
                "task_id": row["task_id"], "original_run_id": int(row["id"]),
                "classification": "environment_recoverable", "assignee": RECOVERY_ASSIGNEE,
                "reason": repair_detail, "attempt": 0, "automatic_retry": True,
            }
        return _defer_to_operator_repair(
            db_path, row, classification=f"browser_infrastructure: {repair_detail}"
        )
    active = [item for item in previous if item["status"] == "scheduled"]
    if active:
        latest = active[-1]
        return {
            "action": "goal_task_recovery_wait", "goal_id": row["goal_id"],
            "task_id": row["task_id"], "original_run_id": int(row["id"]),
            "recovery_task_id": latest["recovery_task_id"],
            "attempt": int(latest["attempt"]), "classification": latest["classification"],
        }

    if not previous and _direct_requeue_is_safe(row, classification, detail):
        BUS.acknowledge_goal_task_run(
            db_path, run_id=int(row["id"]), changed_by="recovery",
            note=(
                f"Self-healing classified {row.get('error_code')} as {classification}; "
                "no uncertain repository execution needs replay. Reassigning to ChatGPT."
            ),
            requeue=True, reassign_to=RECOVERY_ASSIGNEE,
        )
        return {
            "action": "goal_task_recovery_resumed", "goal_id": row["goal_id"],
            "task_id": row["task_id"], "original_run_id": int(row["id"]),
            "classification": classification, "assignee": RECOVERY_ASSIGNEE,
            "reason": _public_reason(row, classification), "attempt": 0,
        }

    attempts = len(previous)
    if attempts >= MAX_RECOVERY_ATTEMPTS:
        return _defer_to_operator_repair(
            db_path, row,
            classification=classification,
        )
    attempt = attempts + 1
    recovery_assignee = RECOVERY_ASSIGNEES[min(attempt - 1, len(RECOVERY_ASSIGNEES) - 1)]
    description = _build_recovery_description(
        row=row, classification=classification, fingerprint=fingerprint,
        attempt=attempt, detail=detail,
    )
    scheduled = BUS.schedule_goal_task_recovery(
        db_path, original_run_id=int(row["id"]), classification=classification,
        fingerprint=fingerprint, attempt=attempt, assignee=recovery_assignee,
        description=description,
    )
    return {
        **scheduled,
        "reason": _public_reason(row, classification),
        "max_attempts": MAX_RECOVERY_ATTEMPTS,
        "automatic_retry": False,
    }


def _handle_failed_recovery_run(
    db_path: Path, *, row: dict[str, Any], relation: dict[str, Any]
) -> dict[str, Any]:
    if relation["status"] != "scheduled":
        return {
            "action": "goal_task_recovery_not_needed", "run_id": int(row["id"]),
            "recovery_id": int(relation["id"]), "status": relation["status"],
        }
    BUS.update_goal_task_recovery(db_path, recovery_id=int(relation["id"]), status="failed")
    detail = _error_detail(db_path, row)
    classification = classify_failure(
        error_code=row.get("error_code") or "", assignee=row["assignee"], detail=detail
    )
    BUS.acknowledge_goal_task_run(
        db_path, run_id=int(row["id"]), changed_by="recovery",
        note="Recovery attempt failed; preserving its evidence and advancing the bounded recovery loop.",
        requeue=False,
    )
    state = BUS.goal_status(db_path, goal_id=row["goal_id"])
    task = next(item for item in state["tasks"] if item["task_id"] == row["task_id"])
    if task["status"] == "in_progress":
        BUS.transition_goal_task(
            db_path, goal_id=row["goal_id"], task_id=row["task_id"], status="blocked",
            changed_by="recovery", blocker=_public_reason(row, classification),
        )
    return recover_run(db_path, run_id=int(relation["original_run_id"]))


def _terminal_recovery_task(
    db_path: Path, *, relation: dict[str, Any], task: dict[str, Any]
) -> dict[str, Any]:
    original = _run_row(db_path, int(relation["original_run_id"]))
    if task["status"] == "completed":
        BUS.update_goal_task_recovery(db_path, recovery_id=int(relation["id"]), status="resolved")
        if original["status"] == "attention_required":
            BUS.acknowledge_goal_task_run(
                db_path, run_id=int(original["id"]), changed_by="recovery",
                note=(
                    f"Recovery task {relation['recovery_task_id']} completed; current state was "
                    "inspected/remediated and the original task is safe to resume."
                ),
                requeue=True, reassign_to=RECOVERY_ASSIGNEE,
            )
        return {
            "action": "goal_task_recovery_resumed", "goal_id": original["goal_id"],
            "task_id": original["task_id"], "original_run_id": int(original["id"]),
            "recovery_task_id": relation["recovery_task_id"],
            "attempt": int(relation["attempt"]), "classification": relation["classification"],
            "assignee": RECOVERY_ASSIGNEE,
        }
    if task["status"] == "user_decision_required":
        BUS.update_goal_task_recovery(db_path, recovery_id=int(relation["id"]), status="escalated")
        return _escalate_original(
            db_path, original,
            reason=(
                f"Recovery task {relation['recovery_task_id']} determined that a user-only decision "
                "or irreversible external action is required."
            ),
        )
    if task["status"] == "blocked":
        BUS.update_goal_task_recovery(db_path, recovery_id=int(relation["id"]), status="failed")
        return recover_run(db_path, run_id=int(relation["original_run_id"]))
    return {
        "action": "goal_task_recovery_wait", "goal_id": relation["goal_id"],
        "task_id": relation["original_task_id"],
        "original_run_id": int(relation["original_run_id"]),
        "recovery_task_id": relation["recovery_task_id"],
        "attempt": int(relation["attempt"]),
    }


def sweep(db_path: Path, *, limit: int = 20) -> dict[str, Any]:
    """Advance durable recovery state after crashes, failures, or task completion."""
    BUS.initialize(db_path)
    actions: list[dict[str, Any]] = []
    with BUS.connect(db_path) as db:
        terminal = db.execute(
            """SELECT r.*, t.status AS recovery_task_status
               FROM goal_task_recoveries r
               JOIN goal_tasks t ON t.goal_id = r.goal_id AND t.task_id = r.recovery_task_id
               WHERE r.status = 'scheduled'
                 AND t.status IN ('completed', 'blocked', 'user_decision_required')
               ORDER BY r.id LIMIT ?""",
            (int(limit),),
        ).fetchall()
    for item in terminal:
        relation = dict(item)
        task = {"status": relation.pop("recovery_task_status")}
        actions.append(_terminal_recovery_task(db_path, relation=relation, task=task))

    remaining = max(0, int(limit) - len(actions))
    if remaining:
        with BUS.connect(db_path) as db:
            attention = db.execute(
                """SELECT id FROM goal_task_runs
                   WHERE status = 'attention_required' ORDER BY id LIMIT ?""",
                (remaining,),
            ).fetchall()
        for item in attention:
            actions.append(recover_run(db_path, run_id=int(item["id"])))
    return {"action": "goal_task_recovery_sweep", "actions": actions}
