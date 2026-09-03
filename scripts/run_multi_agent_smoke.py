#!/usr/bin/env python
"""Prove the multi-agent path issues one real submission per role, submitting none.

The question this answers is narrow and specific: does asking for N review roles
produce N *separate* web submissions, or one conversation asked to play N parts?
Unit tests answer it against a fake runner, which cannot rule out the failure
mode - a fake will happily report whatever the caller wants.

So this drives the real Oracle runner, one lane at a time, with `dry_run=True`.
The runner builds the actual argv it would launch and stops at the submission
boundary. That is enough to show N distinct child manifests, N distinct missions,
and N distinct launches, at zero cost and with no web session created.

What it cannot show is that the resulting web conversations carry distinct
conversation ids - that needs real submission. This script never claims it.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE_BIN = ROOT / "bin"

ROLES = ["evidence_researcher", "adversarial_reviewer", "architecture_reviewer"]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def installed_bin() -> Path:
    home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    return home / "bin"


def run_smoke(*, bin_root: Path) -> dict[str, Any]:
    cli = _load("multi_agent_smoke_cli", bin_root / "chatgpt_multi_agent.py")
    runner = cli.ORACLE_MULTI.RUNNER
    checks: list[dict[str, Any]] = []
    launches: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: Any = None) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    with tempfile.TemporaryDirectory(prefix="codex-multi-agent-smoke-") as workspace:
        base = Path(workspace)
        project = base / "project"
        project.mkdir()
        os.environ["CODEX_ORACLE_STATE_ROOT"] = str((base / "host-state").resolve())

        def recording_execute(manifest_path: Path, *, dry_run: bool) -> dict[str, Any]:
            """Call the real runner and keep what it would have launched."""
            result = runner.execute_run(manifest_path, dry_run=dry_run)
            payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            launches.append(
                {
                    "manifest_path": str(Path(manifest_path).resolve()),
                    "mission_path": payload.get("mission_path"),
                    "parent": payload.get("parallel_parent_id"),
                    "argv": [str(item) for item in (result.get("argv") or [])],
                    "ok": bool(result.get("ok")),
                }
            )
            return result

        # The runner requires the output directory to live inside the project
        # root, which is also where the CLI's own default puts it.
        output_dir = project / ".workflow" / "multi-agent"
        plan = cli.build_plan(
            task="Multi-agent smoke. Do nothing.",
            roles=ROLES,
            project_root=project,
            output_dir=output_dir,
            max_concurrency=len(ROLES),
        )
        report = cli.run_plan(plan, execute=recording_execute, dry_run=True)

        expected = len(ROLES) + 1  # workers plus the synthesis session
        record("every_lane_reached_the_real_runner", len(launches) == expected,
               {"expected": expected, "actual": len(launches)})
        record("runner_accepted_every_lane", all(item["ok"] for item in launches))
        record("child_manifests_are_distinct",
               len({item["manifest_path"] for item in launches}) == len(launches))
        record("missions_are_distinct",
               len({item["mission_path"] for item in launches}) == len(launches))
        record("lanes_share_one_parent",
               len({item["parent"] for item in launches}) == 1)

        argvs = [tuple(item["argv"]) for item in launches if item["argv"]]
        record("each_lane_builds_its_own_launch",
               len(argvs) == len(launches) and len(set(argvs)) == len(launches),
               {"argv_count": len(argvs)})
        record("no_lane_submits_a_file",
               all("--file" not in item["argv"] for item in launches))
        record("report_declares_nothing_submitted", report.get("submitted") is False)
        record("report_counts_every_submission",
               report.get("independent_submission_count") == expected)

        missions = {}
        for role in ROLES:
            path = output_dir / "missions" / f"{role}.md"
            missions[role] = path.read_text(encoding="utf-8") if path.is_file() else ""
        record("each_role_gets_its_own_mission_text",
               len({text for text in missions.values() if text}) == len(ROLES))
        record("missions_declare_session_isolation",
               all("independent" in text for text in missions.values()))

    ok = all(item["ok"] for item in checks)
    return {
        "schema": "codex.chatgpt.multi-agent-smoke/v1",
        "ok": ok,
        "bin_root": str(bin_root),
        "submitted_question": False,
        "roles": ROLES,
        "expected_submissions": len(ROLES) + 1,
        "observed_launches": len(launches),
        "checks": checks,
        "failed_checks": [item["check"] for item in checks if not item["ok"]],
        "proves": "one distinct submission per role, built by the real runner",
        "does_not_prove": (
            "that the resulting web conversations carry distinct conversation ids - "
            "that requires real submission"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the no-submission multi-agent independence smoke."
    )
    parser.add_argument("--check-installed", action="store_true")
    args = parser.parse_args(argv)
    bin_root = installed_bin() if args.check_installed else SOURCE_BIN
    result = run_smoke(bin_root=bin_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
