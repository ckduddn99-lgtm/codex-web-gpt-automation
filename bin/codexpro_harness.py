#!/usr/bin/env python3
"""Durable Oracle status-audit and exact-session resume supervisor."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


SCHEMA = "codex.codexpro.harness/v1"
INTERVIEW_SCHEMA = "codex.gjc.interview/v1"
TERMINAL_PHASES = {"COMPLETE", "BLOCKED", "CANCELLED"}
DEFAULT_POLICY = {
    # Compatibility names remain readable in persisted v1 state, but elapsed
    # time never ends work, releases an owner, or authorizes a new episode.
    "soft_checkpoint_seconds": 4800,
    "handoff_seconds": 4800,
    "observed_platform_limit_seconds": 6000,
    "max_total_concurrency": 5,
    "web_answer_budget_seconds": 6000,
    "status_audit_seconds": 4800,
}
SPAWN_TOOLS = {"spawn_agent", "collaborationspawn_agent", "collaboration.spawn_agent"}


class HarnessError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def parse_time(value: str | None) -> datetime:
    if not value:
        return utc_now()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class StateMutex(AbstractContextManager["StateMutex"]):
    def __init__(self, path: Path):
        self.path = path
        self.stream: Any | None = None

    def __enter__(self) -> "StateMutex":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        if os.name == "nt":
            import msvcrt
            if self.path.stat().st_size == 0:
                self.stream.write(b"0")
                self.stream.flush()
            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *args: Any) -> None:
        if self.stream is None:
            return
        if os.name == "nt":
            import msvcrt
            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        self.stream.close()


def state_root(value: Path | None = None) -> Path:
    return (value or Path(os.environ.get("CODEXPRO_HARNESS_STATE") or Path.home() / ".codex" / "state" / "codexpro-harness")).expanduser().resolve()


def project_key(project_root: Path) -> str:
    return hashlib.sha256(str(project_root.resolve()).encode("utf-8")).hexdigest()[:24]


def project_state_dir(root: Path, project_root: Path) -> Path:
    return root / "projects" / project_key(project_root)


def run_dir_for(root: Path, project_root: Path, run_id: str) -> Path:
    return project_state_dir(root, project_root) / "runs" / run_id


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(f"STATE_UNREADABLE: {path}") from exc
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise HarnessError(f"STATE_SCHEMA_INVALID: {path}")
    return value


def _active_state_path(root: Path, project_root: Path) -> Path | None:
    pointer = project_state_dir(root, project_root) / "active.json"
    try:
        value = json.loads(pointer.read_text(encoding="utf-8"))
        candidate = run_dir_for(root, project_root, str(value["run_id"])) / "run.json"
    except (OSError, KeyError, json.JSONDecodeError):
        return None
    return candidate if candidate.is_file() else None


def _write_ledger(directory: Path, event: str, state: dict[str, Any], **details: Any) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "at": iso(utc_now()),
        "event": event,
        "run_id": state["run_id"],
        "generation": state["generation"],
        "phase": state["phase"],
        **details,
    }
    with (directory / "ledger.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _write_handoff(directory: Path, state: dict[str, Any]) -> Path:
    todo = state.get("todo") or []
    oracle = state.get("oracle_sessions") or []
    lines = [
        f"# CodexPro Harness Handoff — {state['run_id']}",
        "",
        f"- Phase: `{state['phase']}`",
        f"- Generation: `{state['generation']}`",
        f"- Project: `{state['project_root']}`",
        f"- Mission: `{state['mission']['path']}`",
        f"- Mission SHA-256: `{state['mission']['sha256']}`",
        f"- Codex session: `{state.get('codex_session_id') or 'unknown'}`",
        f"- Next instruction: {state.get('next_instruction') or 'Reload the durable todo ledger and continue the next incomplete item.'}",
        "",
        "## Todo",
        *([f"- [{'x' if item.get('complete') else ' '}] {item.get('text')}" for item in todo] or ["- [ ] Reload OMO goal status and continue."]),
        "",
        "## Exact Oracle sessions",
        *([f"- `{item.get('slug')}` — `{item.get('conversation_url') or 'URL pending'}` — terminal={bool(item.get('terminal_observed'))}" for item in oracle] or ["- None"]),
        "",
        "Never resubmit a live Oracle mission. Use the recorded run directory and exact slug recovery.",
    ]
    path = directory / "handoff.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return path


def _discover_codex_owner_pid() -> int | None:
    if os.name == "nt":
        return os.getppid()
    pid = os.getppid()
    for _ in range(8):
        completed = subprocess.run(["ps", "-p", str(pid), "-o", "ppid=", "-o", "comm="], capture_output=True, text=True, check=False)
        if completed.returncode != 0 or not completed.stdout.strip():
            break
        parts = completed.stdout.strip().split(None, 1)
        if len(parts) != 2:
            break
        parent, command = int(parts[0]), parts[1]
        if Path(command).name == "codex":
            return pid
        pid = parent
    return os.getppid()


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def start_run(
    project_root: Path,
    mission_path: Path,
    *,
    root: Path,
    now: datetime | None = None,
    codex_session_id: str | None = None,
    owner_pid: int | None = None,
    next_instruction: str | None = None,
) -> dict[str, Any]:
    project_root = project_root.expanduser().resolve()
    mission_path = mission_path.expanduser().resolve()
    if not project_root.is_dir():
        raise HarnessError("PROJECT_ROOT_INVALID")
    if not mission_path.is_file() or not mission_path.is_relative_to(project_root):
        raise HarnessError("MISSION_MUST_BE_A_PROJECT_FILE")
    directory = project_state_dir(root, project_root)
    with StateMutex(directory / ".state.lock"):
        active_path = _active_state_path(root, project_root)
        if active_path is not None:
            active = _load(active_path)
            if active.get("phase") not in TERMINAL_PHASES:
                raise HarnessError(f"ACTIVE_RUN_EXISTS: {active['run_id']}")
        run_id = uuid.uuid4().hex
        started = now or utc_now()
        run_directory = run_dir_for(root, project_root, run_id)
        session_id = codex_session_id or os.environ.get("CODEX_THREAD_ID")
        state: dict[str, Any] = {
            "schema": SCHEMA,
            "run_id": run_id,
            "generation": 1,
            "phase": "RUNNING",
            "project_root": str(project_root),
            "mission": {"path": str(mission_path), "sha256": sha256_file(mission_path)},
            "codex_session_id": session_id,
            "owner_pid": owner_pid or _discover_codex_owner_pid(),
            "owner_released": False,
            "started_at": iso(started),
            "episode_started_at": iso(started),
            "last_heartbeat_at": iso(started),
            "fanout_locked": False,
            "policy": dict(DEFAULT_POLICY),
            "omo": {
                "boulder_path": str(project_root / ".omo" / "boulder.json"),
                "ledger_root": str(project_root / ".omo" / "ulw-loop"),
            },
            "todo": [],
            "evidence_hashes": [],
            "terminal_observations": [],
            "oracle_sessions": [],
            "next_instruction": next_instruction or "Reload OMO status, inspect the handoff, and continue the first incomplete criterion.",
            "resume_attempt": None,
            "created_at": iso(started),
            "updated_at": iso(started),
        }
        write_json_atomic(run_directory / "run.json", state)
        write_json_atomic(directory / "active.json", {"schema": "codex.codexpro.harness-active/v1", "run_id": run_id})
        _write_handoff(run_directory, state)
        _write_ledger(run_directory, "run_started", state)
        return state


def _save(path: Path, state: dict[str, Any], event: str, **details: Any) -> dict[str, Any]:
    state["updated_at"] = iso(utc_now())
    write_json_atomic(path, state)
    _write_handoff(path.parent, state)
    _write_ledger(path.parent, event, state, **details)
    return state


def _oracle_terminal(item: dict[str, Any]) -> bool:
    if item.get("terminal_observed"):
        return True
    directory = Path(str(item.get("run_dir") or ""))
    try:
        value = json.loads((directory / "run.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return str(value.get("session_authority") or "") in {"terminal", "terminal_observed"} and str(value.get("transport_status") or "") == "complete"


def _evaluate_unlocked(path: Path, *, now: datetime | None = None) -> dict[str, Any]:
    current = now or utc_now()
    state = _load(path)
    if state["phase"] in TERMINAL_PHASES:
        return state
    elapsed = max(0.0, (current - parse_time(state.get("episode_started_at"))).total_seconds())
    policy = {**DEFAULT_POLICY, **(state.get("policy") or {})}
    if elapsed >= float(policy["status_audit_seconds"]):
        prior = state.get("status_audit") if isinstance(state.get("status_audit"), dict) else {}
        threshold = float(policy["status_audit_seconds"])
        audit_count = max(1, int(elapsed // threshold))
        if int(prior.get("audit_count") or 0) < audit_count:
            live = [item for item in state.get("oracle_sessions") or [] if not _oracle_terminal(item)]
            if live:
                state["phase"] = "RECOVER_SAME_SESSION"
                state["recovery_targets"] = [
                    {key: item.get(key) for key in ("run_dir", "slug", "conversation_url")}
                    for item in live
                ]
            state["status_audit"] = {
                "threshold_kind": "caution-status-audit",
                "threshold_seconds": threshold,
                "audit_count": audit_count,
                "observed_at": iso(current),
                "phase_observed": state["phase"],
                "decision": (
                    "continue-exact-session-recovery"
                    if live
                    else "preserve-current-owner-and-exact-sessions"
                ),
                "time_alone_is_terminal": False,
                "new_submission_authorized": False,
            }
            _save(path, state, "status_audit_recorded", elapsed_seconds=elapsed)
    if state["phase"] == "RECOVER_SAME_SESSION":
        live = [item for item in state.get("oracle_sessions") or [] if not _oracle_terminal(item)]
        if not live:
            state["phase"] = "READY_NEXT_EPISODE" if state.get("owner_released") else "HANDOFF_PENDING"
            state["recovery_targets"] = []
            state["resume_attempt"] = None
            _save(path, state, "oracle_recovery_terminal_observed", elapsed_seconds=elapsed)
    return state


def evaluate(path: Path, *, now: datetime | None = None) -> dict[str, Any]:
    with StateMutex(path.parent / ".run.lock"):
        return _evaluate_unlocked(path, now=now)


def register_oracle(path: Path, *, oracle_run_dir: Path, slug: str, conversation_url: str | None, terminal_observed: bool) -> dict[str, Any]:
    with StateMutex(path.parent / ".run.lock"):
        state = _load(path)
        directory = oracle_run_dir.expanduser().resolve()
        records = list(state.get("oracle_sessions") or [])
        replacement = {
            "run_dir": str(directory),
            "slug": slug,
            "conversation_url": conversation_url,
            "terminal_observed": terminal_observed,
        }
        records = [item for item in records if item.get("run_dir") != str(directory)] + [replacement]
        state["oracle_sessions"] = records
        return _save(path, state, "oracle_registered", slug=slug, terminal_observed=terminal_observed)


def release_owner(path: Path, *, session_id: str | None = None) -> dict[str, Any]:
    with StateMutex(path.parent / ".run.lock"):
        state = _load(path)
        if session_id and state.get("codex_session_id") not in {None, session_id}:
            raise HarnessError("SESSION_OWNERSHIP_MISMATCH")
        state["owner_released"] = True
        state["owner_released_at"] = iso(utc_now())
        return _save(path, state, "owner_released")


def heartbeat(path: Path, *, session_id: str | None = None) -> dict[str, Any]:
    with StateMutex(path.parent / ".run.lock"):
        state = _load(path)
        state["last_heartbeat_at"] = iso(utc_now())
        if session_id:
            state["codex_session_id"] = session_id
        state["owner_pid"] = _discover_codex_owner_pid()
        state["owner_released"] = False
        return _save(path, state, "heartbeat")


def session_started(path: Path, *, session_id: str | None = None) -> dict[str, Any]:
    with StateMutex(path.parent / ".run.lock"):
        state = _load(path)
        state["last_heartbeat_at"] = iso(utc_now())
        if session_id:
            state["codex_session_id"] = session_id
        state["owner_pid"] = _discover_codex_owner_pid()
        state["owner_released"] = False
        event = "heartbeat"
        if state["phase"] == "RESUME_STARTED":
            state["generation"] = int(state["generation"]) + 1
            state["phase"] = "RUNNING"
            state["episode_started_at"] = iso(utc_now())
            state["fanout_locked"] = False
            state["resume_attempt"] = None
            event = "next_episode_started"
        return _save(path, state, event)


def _resume_prompt(state: dict[str, Any], handoff: Path) -> str:
    return (
        "CodexPro harness safe-resume. Read the durable handoff at "
        f"{handoff}. Continue generation {int(state['generation']) + 1} from the first incomplete todo. "
        "Do not resubmit any live Oracle run; recover only its recorded exact slug and conversation URL."
    )


def execute_resume(path: Path) -> dict[str, Any]:
    with StateMutex(path.parent / ".run.lock"):
        state = _evaluate_unlocked(path)
        if state["phase"] == "RECOVER_SAME_SESSION":
            targets = state.get("recovery_targets") or []
            if not targets:
                raise HarnessError("RECOVERY_TARGET_MISSING")
            if state.get("resume_attempt") and state["resume_attempt"].get("generation") == state["generation"]:
                return state
            target = targets[0]
            log = path.parent / "oracle-recovery.log"
            stream = log.open("ab")
            command = [
                sys.executable,
                str(Path(__file__).resolve().with_name("chatgpt_oracle_run.py")),
                "recover", "--run-dir", str(target["run_dir"]), "--action", "live",
                "--settle-timeout-seconds", "1200",
            ]
            process = subprocess.Popen(command, cwd=state["project_root"], stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT, start_new_session=os.name != "nt")
            stream.close()
            state["resume_attempt"] = {"generation": state["generation"], "kind": "oracle_exact_recovery", "pid": process.pid, "started_at": iso(utc_now())}
            return _save(path, state, "exact_recovery_started", pid=process.pid)
        if state["phase"] != "READY_NEXT_EPISODE":
            return state
        if not state.get("owner_released"):
            raise HarnessError("OWNER_NOT_RELEASED")
        session_id = str(state.get("codex_session_id") or "").strip()
        if not session_id:
            state["phase"] = "BLOCKED"
            state["blocker"] = "CODEX_SESSION_ID_MISSING"
            return _save(path, state, "resume_blocked")
        if state.get("resume_attempt") and state["resume_attempt"].get("generation") == state["generation"]:
            return state
        handoff = _write_handoff(path.parent, state)
        log = path.parent / "codex-resume.jsonl"
        stream = log.open("ab")
        command = ["codex", "exec", "resume", "--json", session_id, _resume_prompt(state, handoff)]
        process = subprocess.Popen(command, cwd=state["project_root"], stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT, start_new_session=os.name != "nt")
        stream.close()
        state["phase"] = "RESUME_STARTED"
        state["resume_attempt"] = {"generation": state["generation"], "kind": "codex_exact_resume", "pid": process.pid, "started_at": iso(utc_now())}
        return _save(path, state, "codex_resume_started", pid=process.pid)


def find_states(root: Path) -> Iterable[Path]:
    return sorted(root.glob("projects/*/runs/*/run.json")) if root.is_dir() else ()


def doctor(root: Path) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    for path in find_states(root):
        try:
            state = _load(path)
            policy = {**DEFAULT_POLICY, **(state.get("policy") or {})}
            ordered = (
                60 <= int(policy["status_audit_seconds"]) <= int(policy["observed_platform_limit_seconds"])
                and 60 <= int(policy["web_answer_budget_seconds"]) <= int(policy["observed_platform_limit_seconds"])
            )
            concurrency_ok = 1 <= int(policy["max_total_concurrency"]) <= 5
            reports.append({
                "ok": ordered and concurrency_ok,
                "run_id": state.get("run_id"),
                "phase": state.get("phase"),
                "path": str(path),
                "policy_ordered": ordered,
                "concurrency_bounded": concurrency_ok,
            })
        except (HarnessError, KeyError, TypeError, ValueError) as exc:
            reports.append({"ok": False, "path": str(path), "error": str(exc)})
    return {
        "ok": all(item["ok"] for item in reports),
        "schema": SCHEMA,
        "state_root": str(root),
        "runs": reports,
    }


def supervise(root: Path, *, now: datetime | None = None, execute: bool = False) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for path in find_states(root):
        try:
            state = evaluate(path, now=now)
            if execute and state["phase"] in {"RECOVER_SAME_SESSION", "READY_NEXT_EPISODE"}:
                state = execute_resume(path)
            reports.append({"ok": True, "run_id": state["run_id"], "phase": state["phase"], "path": str(path)})
        except HarnessError as exc:
            reports.append({"ok": False, "path": str(path), "error": str(exc)})
    return reports


def active_for_cwd(root: Path, cwd: Path) -> tuple[Path, dict[str, Any]] | None:
    current = cwd.expanduser().resolve()
    while True:
        path = _active_state_path(root, current)
        if path is not None:
            return path, _load(path)
        if current.parent == current:
            return None
        current = current.parent


def run_hook(kind: str, payload: dict[str, Any], *, root: Path) -> str:
    cwd = Path(str(payload.get("cwd") or "."))
    active = active_for_cwd(root, cwd)
    if active is None:
        return ""
    path, state = active
    session_id = str(payload.get("session_id") or "").strip() or None
    if kind == "session-start":
        state = session_started(path, session_id=session_id)
        if state["phase"] in {"CHECKPOINT_DUE", "HANDOFF_PENDING", "RECOVER_SAME_SESSION", "READY_NEXT_EPISODE"}:
            return f"<codexpro-harness-resume>Read {path.parent / 'handoff.md'} and continue exact state; never resubmit a live Oracle run.</codexpro-harness-resume>"
        return ""
    state = evaluate(path)
    if kind == "stop":
        if state["phase"] != "RUNNING":
            release_owner(path, session_id=session_id)
        return ""
    if kind == "pre-tool-use" and state.get("fanout_locked") and str(payload.get("tool_name") or "") in SPAWN_TOOLS:
        reason = "A manual harness checkpoint is active; finish the handoff before spawning more agents."
        return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason, "additionalContext": reason}})
    return ""


def _resolve_run_path(args: argparse.Namespace) -> Path:
    if args.run_path:
        return args.run_path.expanduser().resolve()
    root = state_root(args.state_root)
    active = _active_state_path(root, args.project_root.expanduser().resolve())
    if active is None:
        raise HarnessError("ACTIVE_RUN_MISSING")
    return active


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("--project-root", type=Path, required=True)
    start.add_argument("--mission-path", type=Path, required=True)
    start.add_argument("--codex-session-id")
    start.add_argument("--owner-pid", type=int)
    start.add_argument("--next-instruction")
    for name in ("status", "checkpoint", "release", "heartbeat", "resume"):
        value = commands.add_parser(name)
        value.add_argument("--run-path", type=Path)
        value.add_argument("--project-root", type=Path, default=Path.cwd())
        if name == "resume":
            value.add_argument("--execute", action="store_true")
    oracle = commands.add_parser("register-oracle")
    oracle.add_argument("--run-path", type=Path)
    oracle.add_argument("--project-root", type=Path, default=Path.cwd())
    oracle.add_argument("--oracle-run-dir", type=Path, required=True)
    oracle.add_argument("--slug", required=True)
    oracle.add_argument("--conversation-url")
    oracle.add_argument("--terminal-observed", action="store_true")
    supervise_parser = commands.add_parser("supervise")
    supervise_parser.add_argument("--execute-resume", action="store_true")
    supervise_parser.add_argument("--now")
    hook = commands.add_parser("hook")
    hook.add_argument("kind", choices=("session-start", "stop", "pre-tool-use"))
    commands.add_parser("doctor")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = state_root(args.state_root)
    try:
        if args.command == "start":
            result: Any = start_run(args.project_root, args.mission_path, root=root, codex_session_id=args.codex_session_id, owner_pid=args.owner_pid, next_instruction=args.next_instruction)
        elif args.command == "supervise":
            result = {"ok": True, "runs": supervise(root, now=parse_time(args.now) if args.now else None, execute=args.execute_resume)}
        elif args.command == "doctor":
            result = doctor(root)
        elif args.command == "hook":
            try:
                payload = json.load(sys.stdin)
            except json.JSONDecodeError:
                return 0
            output = run_hook(args.kind, payload, root=root)
            if output:
                print(output)
            return 0
        else:
            path = _resolve_run_path(args)
            if args.command == "status":
                result = evaluate(path)
            elif args.command == "checkpoint":
                with StateMutex(path.parent / ".run.lock"):
                    state = _load(path)
                    state["phase"] = "CHECKPOINT_DUE"
                    state["fanout_locked"] = True
                    result = _save(path, state, "manual_checkpoint")
            elif args.command == "release":
                result = release_owner(path, session_id=os.environ.get("CODEX_THREAD_ID"))
            elif args.command == "heartbeat":
                result = heartbeat(path, session_id=os.environ.get("CODEX_THREAD_ID"))
            elif args.command == "register-oracle":
                result = register_oracle(path, oracle_run_dir=args.oracle_run_dir, slug=args.slug, conversation_url=args.conversation_url, terminal_observed=args.terminal_observed)
            else:
                result = execute_resume(path) if args.execute else evaluate(path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except HarnessError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
