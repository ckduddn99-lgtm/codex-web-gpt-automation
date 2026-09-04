#!/usr/bin/env python
"""Role-driven front end for the Oracle multi runner.

`chatgpt_oracle_multi.py` already owns the hard part: it creates one independent
web session per lane, bounds concurrency, splits the lanes into waves, enforces
per-writer worktree ownership, collects results in solver order, and merges the
handoffs.  What it did not have was a way to ask for that in the vocabulary
people actually use - "review this with an evidence researcher, an adversarial
reviewer, and an operations risk reviewer".

This module is that front end and nothing more.  It renders one mission per role
from the shared prompt profiles, writes a manifest the existing engine already
understands, delegates the whole run to `run_multi`, and reports what came back.
No scheduling, isolation, or merge logic is reimplemented here.

The one judgment it adds is a refusal: if the workers did not actually end up in
distinct web conversations, the run is reported as failed rather than as a
multi-agent success.  Several personas taking turns inside a single conversation
is the exact failure this surface exists to rule out.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


BIN = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:  # pragma: no cover - packaging failure
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PROFILES = _load("chatgpt_multi_agent_profiles", BIN / "chatgpt_prompt_profiles.py")
REDACTION = _load("chatgpt_multi_agent_redaction", BIN / "chatgpt_log_redaction.py")
ORACLE_MULTI = _load("chatgpt_multi_agent_oracle_multi", BIN / "chatgpt_oracle_multi.py")
PREFLIGHT = _load("chatgpt_multi_agent_preflight", BIN / "chatgpt_devspace_preflight.py")

PLAN_SCHEMA = "codex.chatgpt.multi-agent-plan/v1"
REPORT_SCHEMA = "codex.chatgpt.multi-agent-report/v1"
DEFAULT_MAX_CONCURRENCY = 5
MODES = ("analysis", "implementation")


class MultiAgentError(RuntimeError):
    pass


ANALYSIS_OUTPUT_CONTRACT = (
    "Return findings for your role only. Each finding needs a claim, the specific evidence "
    "you observed, and its concrete impact. Mark anything you could not verify as unverified "
    "instead of asserting it. Do not review the other roles' territory and do not restate the task."
)

SYNTHESIS_OUTPUT_CONTRACT = (
    "Compare the attached worker handoffs. Resolve conflicts between them explicitly rather than "
    "averaging or concatenating. Name every worker that failed, timed out, or is missing, and state "
    "what its absence leaves unverified. Do not present a partial set as a complete review."
)

IMPLEMENTATION_OUTPUT_CONTRACT = (
    "Change only the files you own. Do not modify Git refs, the index, branches, or worktrees. "
    "Report the exact paths you touched and the verification you actually ran."
)


def _mission_text(task: str, role: str, mode: str, owned_paths: Sequence[str] | None) -> str:
    """Render one role's mission from the shared cognitive profiles."""
    if mode == "implementation":
        profile_name = "edit"
        contract = IMPLEMENTATION_OUTPUT_CONTRACT
        if owned_paths:
            contract = f"{contract}\nFiles you own: {', '.join(owned_paths)}."
    else:
        profile_name = PROFILES.resolve_role(role).name
        contract = (
            SYNTHESIS_OUTPUT_CONTRACT
            if role == PROFILES.DEFAULT_SYNTHESIS_ROLE
            else ANALYSIS_OUTPUT_CONTRACT
        )
    return PROFILES.render_prompt(
        profile_name,
        original_task=task,
        stage_mission=(
            f"You are the {role} worker in a parallel review. You are one of several independent "
            "web sessions examining the same task; you cannot see the other workers and must not "
            "speculate about their conclusions."
        ),
        output_instructions=contract,
        context_note=(
            "Your conversation is isolated. Nothing you write here reaches the other workers "
            "directly - a separate synthesis session compares the handoffs afterwards."
        ),
    )


def _validate_roles(roles: Sequence[str], mode: str) -> list[str]:
    cleaned = [str(item).strip() for item in roles if str(item).strip()]
    if len(cleaned) < 2:
        raise MultiAgentError("a multi-agent run needs at least two worker roles")
    if len(set(cleaned)) != len(cleaned):
        raise MultiAgentError("worker roles must be unique")
    if mode == "analysis":
        for role in cleaned:
            if role not in PROFILES.MULTI_AGENT_ROLES:
                raise MultiAgentError(f"unknown analysis role: {role}")
        if PROFILES.DEFAULT_SYNTHESIS_ROLE in cleaned:
            raise MultiAgentError(
                f"{PROFILES.DEFAULT_SYNTHESIS_ROLE} runs as the synthesis session, not as a worker"
            )
    return cleaned


def _validate_worktrees(
    roles: Sequence[str], worktrees: Mapping[str, Path] | None, mode: str
) -> dict[str, Path]:
    if mode != "implementation":
        if worktrees:
            raise MultiAgentError("analysis workers are read-only and take no worktree")
        return {}
    if not worktrees:
        raise MultiAgentError("implementation workers require a pre-created worktree each")
    missing = [role for role in roles if role not in worktrees]
    if missing:
        raise MultiAgentError(f"implementation roles without a worktree: {', '.join(missing)}")
    resolved = {role: Path(worktrees[role]).resolve() for role in roles}
    if len({str(path) for path in resolved.values()}) != len(resolved):
        # Two writers in one worktree is the concurrent-clobber bug this whole
        # surface exists to prevent; it is never a recoverable warning.
        raise MultiAgentError("implementation workers must not share a worktree")
    return resolved


def build_plan(
    *,
    task: str,
    roles: Sequence[str],
    project_root: Path,
    output_dir: Path,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    mode: str = "analysis",
    worktrees: Mapping[str, Path] | None = None,
    owned_paths: Mapping[str, Sequence[str]] | None = None,
    lane_timeout_seconds: float | None = None,
    app_name: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Write the mission files and manifest for one multi-agent run."""
    if mode not in MODES:
        raise MultiAgentError(f"unknown mode: {mode}")
    if not str(task).strip():
        raise MultiAgentError("task must not be empty")
    if not 1 <= int(max_concurrency) <= DEFAULT_MAX_CONCURRENCY:
        raise MultiAgentError(f"max_concurrency must be within 1..{DEFAULT_MAX_CONCURRENCY}")

    cleaned = _validate_roles(roles, mode)
    resolved_worktrees = _validate_worktrees(cleaned, worktrees, mode)

    project_root = Path(project_root).resolve()
    output_dir = Path(output_dir).resolve()
    missions_dir = output_dir / "missions"
    missions_dir.mkdir(parents=True, exist_ok=True)

    solvers: list[dict[str, Any]] = []
    for role in cleaned:
        owned = list((owned_paths or {}).get(role) or [])
        mission_path = missions_dir / f"{role}.md"
        mission_path.write_text(_mission_text(task, role, mode, owned), encoding="utf-8")
        lane: dict[str, Any] = {"id": role, "mission_path": str(mission_path)}
        if mode == "implementation":
            lane["project_root"] = str(resolved_worktrees[role])
            lane["access"] = "worktree-write"
            lane["owned_paths"] = owned
        else:
            lane["access"] = "read-only"
        solvers.append(lane)

    merger_path = missions_dir / "synthesis.md"
    merger_path.write_text(
        _mission_text(task, PROFILES.DEFAULT_SYNTHESIS_ROLE, "analysis", None), encoding="utf-8"
    )

    manifest_payload: dict[str, Any] = {
        "schema": ORACLE_MULTI.SCHEMA,
        "project_root": str(project_root),
        "output_dir": str(output_dir),
        "app_name": app_name or "DevSpace",
        "model": model or "gpt-5.6",
        "max_concurrency": int(max_concurrency),
        "solvers": solvers,
        "merger_mission_path": str(merger_path),
    }
    if lane_timeout_seconds is not None:
        manifest_payload["lane_timeout_seconds"] = float(lane_timeout_seconds)
    if mode == "implementation":
        manifest_payload["allowed_worktree_roots"] = [
            str(path) for path in resolved_worktrees.values()
        ]

    manifest_path = output_dir / "multi-agent.json"
    manifest_path.write_text(
        json.dumps(manifest_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    step = int(max_concurrency)
    waves = [
        {"index": index // step, "lane_ids": cleaned[index : index + step]}
        for index in range(0, len(cleaned), step)
    ]
    return {
        "schema": PLAN_SCHEMA,
        "mode": mode,
        "task": task,
        "project_root": str(project_root),
        "manifest_path": str(manifest_path),
        "output_dir": str(output_dir),
        "max_concurrency": int(max_concurrency),
        "lane_timeout_seconds": lane_timeout_seconds,
        "waves": waves,
        "workers": [
            {
                "role": role,
                "mission_path": str(missions_dir / f"{role}.md"),
                "access": "worktree-write" if mode == "implementation" else "read-only",
                "worktree": str(resolved_worktrees[role]) if mode == "implementation" else None,
            }
            for role in cleaned
        ],
        "synthesis_mission_path": str(merger_path),
    }


def _synthesis_path(result: Mapping[str, Any], plan: Mapping[str, Any]) -> str | None:
    run_dir = result.get("merger_run_dir")
    if run_dir:
        candidate = Path(str(run_dir)) / "output.md"
        if candidate.is_file():
            return str(candidate)
    return result.get("merger_mission_path") or plan.get("synthesis_mission_path")


def run_plan(
    plan: Mapping[str, Any],
    *,
    execute: Callable[..., dict[str, Any]] | None = None,
    cancel_event: Any | None = None,
    dry_run: bool = False,
    skip_preflight: bool = False,
    preflight_verifier: Callable[[Path], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Delegate the plan to the Oracle multi runner and report what came back.

    A dry run still walks the whole per-lane path - one child manifest and one
    runner invocation per role - and stops at the submission boundary. It is the
    cheapest evidence that N roles become N separate submissions rather than one
    conversation answering N times.
    """
    if not skip_preflight:
        verifier = preflight_verifier or (
            PREFLIGHT.ensure_exact_root_qualified if execute is None else None
        )
        if verifier is not None:
            project_root = Path(str(plan.get("project_root") or Path.cwd())).resolve()
            try:
                verifier(project_root)
                if plan.get("mode") == "implementation":
                    for worker in plan.get("workers", []):
                        worktree_path = worker.get("worktree")
                        if worktree_path:
                            wt = Path(str(worktree_path)).resolve()
                            if not wt.is_dir():
                                raise MultiAgentError(f"worktree does not exist: {wt}")
            except Exception as exc:
                return {
                    "schema": REPORT_SCHEMA,
                    "mode": plan.get("mode"),
                    "ok": False,
                    "status": "preflight_failed",
                    "requested_worker_count": len(plan.get("workers", [])),
                    "independent_session_count": 0,
                    "independent_submission_count": 0,
                    "submitted": False,
                    "waves": plan.get("waves"),
                    "workers": [],
                    "failed_roles": [item["role"] for item in plan.get("workers", [])],
                    "abandoned_lane_ids": [],
                    "synthesis_path": None,
                    "result_path": str(Path(str(plan["output_dir"])) / "result.json"),
                    "error": {
                        "code": "MULTI_AGENT_PREFLIGHT_FAILED",
                        "message": str(exc),
                    },
                }

    kwargs: dict[str, Any] = {"dry_run": bool(dry_run)}
    if execute is not None:
        kwargs["execute"] = execute
    if cancel_event is not None:
        kwargs["cancel_event"] = cancel_event
    result = ORACLE_MULTI.run_multi(Path(str(plan["manifest_path"])), **kwargs)

    workers = [
        {
            "role": lane["id"],
            "status": lane.get("status") or ("complete" if lane.get("ok") else "failed"),
            "ok": bool(lane.get("ok")),
            "session_locator": lane.get("session_locator"),
            "duration_seconds": float(lane.get("duration_seconds") or 0.0),
            "artifact_path": lane.get("output_path"),
        }
        for lane in result.get("lanes", [])
    ]
    sessions = {item["session_locator"] for item in workers if item["session_locator"]}
    completed = [item for item in workers if item["ok"]]
    failed_roles = [item["role"] for item in workers if not item["ok"]]
    abandoned_lane_ids = result.get("abandoned_lane_ids") or []

    is_ok = bool(result.get("ok")) and not failed_roles and not abandoned_lane_ids
    if dry_run:
        is_ok = bool(result.get("ok"))

    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "mode": plan.get("mode"),
        "ok": is_ok,
        "status": result.get("status"),
        "requested_worker_count": len(plan.get("workers", [])),
        "independent_session_count": len(sessions),
        "independent_submission_count": len(workers) + 1,
        "submitted": not dry_run,
        "waves": result.get("waves") or plan.get("waves"),
        "workers": workers,
        "failed_roles": failed_roles,
        "abandoned_lane_ids": abandoned_lane_ids,
        "synthesis_path": _synthesis_path(result, plan),
        "result_path": str(Path(str(plan["output_dir"])) / "result.json"),
    }

    if not dry_run and failed_roles and "error" not in report:
        report["error"] = {
            "code": "MULTI_AGENT_LANES_FAILED",
            "message": f"one or more workers failed: {', '.join(failed_roles)}",
        }

    # Independence is the whole claim of this surface, so it is checked against
    # the sessions that actually reported back rather than assumed from the plan.
    if not dry_run and completed and len(sessions) < len(completed):
        report["ok"] = False
        report["error"] = {
            "code": "MULTI_AGENT_SHARED_SESSION",
            "message": (
                f"{len(completed)} workers reported only {len(sessions)} distinct web sessions; "
                "personas inside one conversation are not independent agents"
            ),
        }
    return report


def _plan_only_report(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": REPORT_SCHEMA,
        "mode": plan.get("mode"),
        "ok": True,
        "status": "plan-only",
        "requested_worker_count": len(plan.get("workers", [])),
        "independent_session_count": 0,
        "independent_submission_count": 0,
        "submitted": False,
        "waves": plan.get("waves"),
        "workers": [
            {
                "role": item["role"],
                "status": "planned",
                "ok": False,
                "session_locator": None,
                "duration_seconds": 0.0,
                "artifact_path": None,
                "mission_path": item["mission_path"],
            }
            for item in plan.get("workers", [])
        ],
        "failed_roles": [],
        "abandoned_lane_ids": [],
        "synthesis_path": plan.get("synthesis_mission_path"),
        "manifest_path": plan.get("manifest_path"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one independent web session per review role and synthesize the handoffs."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run a multi-agent analysis or implementation")
    run.add_argument("--mode", choices=MODES, default="analysis")
    run.add_argument("--task", required=True)
    run.add_argument(
        "--roles",
        default=",".join(PROFILES.DEFAULT_ANALYSIS_ROLES),
        help="comma-separated worker roles (the synthesis session is always added)",
    )
    run.add_argument("--max-concurrency", type=int, default=DEFAULT_MAX_CONCURRENCY)
    run.add_argument("--lane-timeout-seconds", type=float, default=None)
    run.add_argument("--project-root", type=Path, default=Path.cwd())
    run.add_argument("--output-dir", type=Path, default=None)
    run.add_argument("--app-name", default=None)
    run.add_argument("--model", default=None)
    run.add_argument(
        "--plan-only",
        action="store_true",
        help="write the missions and manifest, print the plan, and submit nothing",
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="drive the runner for every role up to the submission boundary and stop",
    )
    run.add_argument(
        "--skip-preflight",
        action="store_true",
        help="skip DevSpace root qualification and preflight checks",
    )
    return parser


def main(
    argv: Iterable[str] | None = None,
    *,
    execute: Callable[..., dict[str, Any]] | None = None,
) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    output_dir = args.output_dir or (Path(args.project_root) / ".workflow" / "multi-agent")
    try:
        plan = build_plan(
            task=args.task,
            roles=[item for item in str(args.roles).split(",")],
            project_root=args.project_root,
            output_dir=output_dir,
            max_concurrency=args.max_concurrency,
            mode=args.mode,
            lane_timeout_seconds=args.lane_timeout_seconds,
            app_name=args.app_name,
            model=args.model,
        )
        report = (
            _plan_only_report(plan)
            if args.plan_only
            else run_plan(
                plan,
                execute=execute,
                dry_run=args.dry_run,
                skip_preflight=args.skip_preflight,
            )
        )
    except Exception as exc:
        report = {
            "schema": REPORT_SCHEMA,
            "ok": False,
            "error": {"code": "MULTI_AGENT_FAILED", "message": str(exc)},
        }
    # Everything printed or persisted here goes through redaction: the report
    # carries session locators and run directories.
    print(json.dumps(REDACTION.redact_payload(report), ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
