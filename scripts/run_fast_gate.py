#!/usr/bin/env python
"""Sub-minute gate for Oracle automation changes.

The full suite takes many minutes, which pushed every repair into one-incident-
at-a-time edits.  This gate covers the contracts that actually broke runs before
submission - launch arguments, lifecycle authority, incident ownership,
compatibility patch shape, and release packaging - and must finish well inside
its wall-clock budget so it can run after every batch of edits.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FAST_TARGETS = [
    "tests/test_chatgpt_oracle_state.py",
    # The full runner module now contains hundreds of exhaustive lifecycle
    # contradiction permutations and takes about 65 seconds by itself.  Keep a
    # bounded cross-section of the launch, ownership, read-gate, completion,
    # restart, and recovery contracts here; the full v4/CI suite still runs
    # every permutation.
    "tests/test_chatgpt_oracle_run.py::test_default_oracle_command_is_pinned_to_the_hash_validated_version",
    "tests/test_chatgpt_oracle_run.py::test_conversation_url_helpers_preserve_exact_binding_and_detect_conflicts",
    "tests/test_chatgpt_oracle_run.py::test_new_runs_use_dynamic_cdp_port_instead_of_global_9222",
    "tests/test_chatgpt_oracle_run.py::test_fresh_execution_binds_runtime_task_but_plain_manifest_loading_stays_unbound",
    "tests/test_chatgpt_oracle_run.py::test_task_outcome_terminal_watchdog_is_exactly_v1_and_scrubs_inherited_state",
    "tests/test_chatgpt_oracle_run.py::test_foreign_task_recovery_is_fail_closed_before_browser_or_oracle_access",
    "tests/test_chatgpt_oracle_run.py::test_legacy_unbound_recovery_is_not_adopted_by_the_current_task",
    "tests/test_chatgpt_oracle_run.py::test_dry_run_never_executes_and_has_no_file_flag",
    "tests/test_chatgpt_oracle_run.py::test_default_signed_in_profile_is_copied_per_run_and_window_is_hidden",
    "tests/test_chatgpt_oracle_run.py::test_regular_runs_use_provider_window_and_nonterminal_status_audit",
    "tests/test_chatgpt_oracle_run.py::test_pro_readonly_dry_run_fails_before_layout_without_fresh_app_read_gate",
    "tests/test_chatgpt_oracle_run.py::test_pro_readonly_dry_run_reports_bound_app_read_gate",
    "tests/test_chatgpt_oracle_run.py::test_prior_pro_app_read_gate_url_does_not_block_thinking_time_pre_submit_settlement",
    "tests/test_chatgpt_oracle_run.py::test_current_run_conversation_url_blocks_thinking_time_pre_submit_settlement",
    "tests/test_chatgpt_oracle_run.py::test_prior_pro_app_read_gate_url_does_not_change_other_pre_submit_failure_settlement",
    "tests/test_chatgpt_oracle_run.py::test_project_session_still_live_settles_only_after_exact_owner_releases",
    "tests/test_chatgpt_oracle_run.py::test_new_writable_pro_manifest_is_rejected_before_layout_or_browser",
    "tests/test_chatgpt_oracle_run.py::test_d_coin_missing_exact_root_blocks_before_oracle_or_run_creation",
    "tests/test_chatgpt_oracle_run.py::test_complete_requires_zero_exit_and_nonempty_output",
    "tests/test_chatgpt_oracle_run.py::test_v1_task_outcome_separates_transport_success_from_execution",
    "tests/test_chatgpt_oracle_run.py::test_devspace_patch_change_blocks_before_submission_until_restart",
    "tests/test_chatgpt_oracle_run.py::test_post_submit_nonzero_requires_exact_recovery_and_never_restarts",
    "tests/test_chatgpt_oracle_run.py::test_recovery_captures_output_and_updates_state",
    "tests/test_chatgpt_oracle_run.py::test_unresolved_exact_session_blocks_different_parent_submission",
    "tests/test_chatgpt_oracle_run.py::test_recovery_never_downgrades_durable_complete",
    "tests/test_chatgpt_oracle_diagnose.py",
    # The parallel multi-agent path owns worktree ownership, wave bounding,
    # lane timeouts, and cancellation.  It was never collected by any gate, so a
    # UTF-8 decode regression sat in it undetected on non-ASCII hosts.
    "tests/test_chatgpt_oracle_multi.py",
    "tests/test_chatgpt_oracle_debate.py",
    "tests/test_chatgpt_research_meeting.py",
    "tests/test_chatgpt_research_meeting_io.py",
    "tests/test_chatgpt_research_meeting_oracle.py",
    "tests/test_chatgpt_log_chat.py",
    "tests/test_chatgpt_multi_agent.py",
    "tests/test_chatgpt_oracle_incident.py",
    "tests/test_chatgpt_oracle_compat.py",
    "tests/test_chatgpt_oracle_profiles.py",
    "tests/test_global_gpt_browser_policy.py",
    "tests/test_release_packaging.py",
    "tests/test_docs_contract.py",
    "tests/test_codex_web_gpt_onboarding.py",
    "tests/test_codex_global_agents_setup.py",
    "tests/test_codex_runtime_identity.py",
    "tests/test_codexpro_cloudflared_launchd.py",
    "tests/test_ultra_economy_mode.py",
]

# This LKG archived-parent DOM integration test intentionally exercises several
# real browser polling windows and takes about 12 seconds by itself. The full
# v4/CI suite still runs it; the fast pre-submit gate keeps the surrounding
# hash/patch/ownership tests while omitting only this terminal follow-up UI
# simulation so normal host jitter cannot consume the entire gate budget.
FAST_DESELECTS = [
    "tests/test_chatgpt_oracle_compat.py::test_archived_parent_direct_restore_requires_exact_control_and_composer_transition",
]

# The onboarding wizard added resumable-state, language, fail-closed
# confirmation, and forged-evidence coverage to this gate, so the wall-clock
# ceiling moved from 60s to 100s.  It must still finish fast enough to run
# after every batch of edits.
DEFAULT_BUDGET_SECONDS = 100.0
# Four-way overlap made Git/worktree-heavy shards slower on the measured Windows
# host; three workers gave the best wall clock without reducing coverage.
DEFAULT_WORKERS = min(3, max(1, os.cpu_count() or 1))
NODE_TARGETS_PER_JOB = 8
LIGHT_FILES_PER_JOB = 4
LONG_JOB_PRIORITY = (
    # Keep the Git/worktree-heavy shard beside the short state/diagnose shards;
    # running it concurrently with debate + research at startup caused severe
    # contention on Windows. Start onboarding first, then admit those two large
    # semantic suites as independent processes instead of bundling them.
    "tests/test_chatgpt_oracle_multi.py",
    "tests/test_chatgpt_oracle_state.py",
    "tests/test_chatgpt_oracle_diagnose.py",
    "tests/test_codex_web_gpt_onboarding.py",
    "tests/test_chatgpt_oracle_debate.py",
    "tests/test_chatgpt_research_meeting.py",
)


def _group_fast_targets() -> list[list[str]]:
    """Schedule broad files early and split only explicit node selections into bounded jobs."""
    grouped: dict[str, list[str]] = {}
    order: list[str] = []
    for target in FAST_TARGETS:
        path = target.split("::", 1)[0]
        if path not in grouped:
            grouped[path] = []
            order.append(path)
        grouped[path].append(target)

    whole_file_jobs: list[list[str]] = []
    node_jobs: list[list[str]] = []
    for path in order:
        targets = grouped[path]
        if path in targets:
            if len(targets) != 1:
                raise RuntimeError(f"fast gate mixes whole-file and node targets: {path}")
            whole_file_jobs.append(targets)
            continue
        for start in range(0, len(targets), NODE_TARGETS_PER_JOB):
            node_jobs.append(targets[start:start + NODE_TARGETS_PER_JOB])
    priority = {path: index for index, path in enumerate(LONG_JOB_PRIORITY)}
    prioritized = [job for job in whole_file_jobs if job[0] in priority]
    prioritized.sort(key=lambda job: priority[job[0]])
    light = [job[0] for job in whole_file_jobs if job[0] not in priority]
    light_jobs = [light[start:start + LIGHT_FILES_PER_JOB]
                  for start in range(0, len(light), LIGHT_FILES_PER_JOB)]
    return prioritized + light_jobs + node_jobs


def _hidden_process_kwargs() -> dict[str, object]:
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startupinfo}


def run_fast_gate(*, budget_seconds: float = DEFAULT_BUDGET_SECONDS,
                  workers: int = DEFAULT_WORKERS) -> dict[str, object]:
    # The caller must also wait for Windows Git/worktree fixture cleanup.
    # Budget the entire invocation, not only the pytest children. File-level
    # jobs preserve the exact test selection while overlapping independent
    # modules; no assertion, skip, deselect, or durability contract is relaxed.
    started = time.monotonic()
    environment = dict(os.environ)
    environment.setdefault("PYTHONUTF8", "1")
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    jobs = _group_fast_targets()
    worker_count = min(max(1, workers), len(jobs))
    def run_job(index: int, targets: list[str]) -> tuple[subprocess.CompletedProcess, float]:
        paths = {target.split("::", 1)[0] for target in targets}
        deselects = [item for item in FAST_DESELECTS if item.split("::", 1)[0] in paths]
        # Give every worker its own temp root so completed shards can clean up
        # while slower tests are still running instead of serializing one large
        # recursive delete after the final child exits.
        with tempfile.TemporaryDirectory(prefix=f"codex-oracle-fast-gate-{index:02d}-") as basetemp:
            command = [
                sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                *targets,
                *(f"--deselect={item}" for item in deselects),
                "--basetemp", basetemp,
            ]
            completed = subprocess.run(
                command,
                cwd=str(ROOT),
                check=False,
                env=environment,
                # Explicit handles are essential with CREATE_NO_WINDOW: implicit
                # inheritance can discard pytest output in a file-backed caller.
                stdout=sys.stdout,
                stderr=sys.stderr,
                **_hidden_process_kwargs(),
            )
            child_finished = time.monotonic()
        return completed, child_finished

    test_started = time.monotonic()
    completed_by_index: dict[int, subprocess.CompletedProcess] = {}
    child_finished_by_index: dict[int, float] = {}
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="fast-gate") as pool:
        futures = {pool.submit(run_job, index, targets): index
                   for index, targets in enumerate(jobs, 1)}
        for future in as_completed(futures):
            index = futures[future]
            completed, child_finished = future.result()
            completed_by_index[index] = completed
            child_finished_by_index[index] = child_finished
    finished = time.monotonic()
    test_finished = max(child_finished_by_index.values(), default=test_started)
    elapsed = finished - started
    exit_codes = [int(completed_by_index[index].returncode) for index in range(1, len(jobs) + 1)]
    exit_code = next((code for code in exit_codes if code != 0), 0)
    return {
        "exit_code": exit_code,
        "child_exit_codes": exit_codes,
        "elapsed_seconds": round(elapsed, 2),
        "test_elapsed_seconds": round(test_finished - test_started, 2),
        "cleanup_seconds": round(finished - test_finished, 2),
        "budget_seconds": budget_seconds,
        "within_budget": elapsed <= budget_seconds,
        "targets": list(FAST_TARGETS),
        "jobs": len(jobs),
        "workers": worker_count,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the sub-minute Oracle automation gate.")
    parser.add_argument("--budget-seconds", type=float, default=DEFAULT_BUDGET_SECONDS)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument(
        "--enforce-budget",
        action="store_true",
        help="Fail when the gate exceeds its wall-clock budget even if tests pass.",
    )
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    print(f"fast-gate start targets={len(FAST_TARGETS)} budget={args.budget_seconds}s workers={args.workers}", flush=True)
    result = run_fast_gate(budget_seconds=args.budget_seconds, workers=args.workers)
    print(
        f"fast-gate exit={result['exit_code']} "
        f"elapsed={result['elapsed_seconds']}s budget={result['budget_seconds']}s "
        f"within_budget={result['within_budget']} "
        f"tests={result['test_elapsed_seconds']}s cleanup={result['cleanup_seconds']}s "
        f"jobs={result['jobs']} workers={result['workers']}",
        flush=True,
    )
    if result["exit_code"] != 0:
        return int(result["exit_code"])
    if args.enforce_budget and not result["within_budget"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
