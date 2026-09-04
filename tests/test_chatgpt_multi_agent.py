"""Contracts for the multi-agent analysis surface layered on the Oracle multi runner.

The Oracle multi runner already creates independent web sessions, bounds
concurrency, splits work into waves, enforces per-writer worktree ownership, and
merges handoffs deterministically.  These tests cover only the parts that were
missing: named review roles, a per-lane timeout that cannot lose sibling
results, run-wide cancellation, secret redaction, and the role-driven CLI.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MULTI_PATH = ROOT / "bin" / "chatgpt_oracle_multi.py"
PROFILES_PATH = ROOT / "bin" / "chatgpt_prompt_profiles.py"
REDACTION_PATH = ROOT / "bin" / "chatgpt_log_redaction.py"
CLI_PATH = ROOT / "bin" / "chatgpt_multi_agent.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_multi():
    return _load("multi_agent_test_oracle_multi", MULTI_PATH)


def load_profiles():
    return _load("multi_agent_test_profiles", PROFILES_PATH)


def load_redaction():
    return _load("multi_agent_test_redaction", REDACTION_PATH)


def load_cli():
    return _load("multi_agent_test_cli", CLI_PATH)


def make_manifest(tmp_path: Path, count: int, **extra) -> Path:
    missions = []
    for index in range(count):
        path = tmp_path / f"solver-{index}.md"
        path.write_text(f"solve {index}", encoding="utf-8")
        missions.append({"id": f"s{index}", "mission_path": str(path.resolve())})
    merger = tmp_path / "merge.md"
    merger.write_text("Merge every listed handoff.", encoding="utf-8")
    manifest = tmp_path / "multi.json"
    payload = {
        "schema": "codex.chatgpt.oracle-multi/v1",
        "project_root": str(tmp_path.resolve()),
        "output_dir": str((tmp_path / "out").resolve()),
        "app_name": "DevSpace",
        "model": "gpt-5.6",
        "max_concurrency": 5,
        "solvers": missions,
        "merger_mission_path": str(merger.resolve()),
    }
    payload.update(extra)
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return manifest


def _ok_execute(session_prefix: str = "https://chatgpt.com/c/session"):
    """A fake runner that behaves like a distinct durable web session per lane."""

    def execute(path: Path, *, dry_run: bool):
        run_dir = path.parent / "fake-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "output.md").write_text(f"answer {path.parent.name}", encoding="utf-8")
        (run_dir / "state.json").write_text(
            json.dumps({"oracle": {"session_locator": f"{session_prefix}-{path.parent.name}"}}),
            encoding="utf-8",
        )
        return {"ok": True, "run_dir": str(run_dir)}

    return execute


# --------------------------------------------------------------------------
# Named review roles
# --------------------------------------------------------------------------


def test_multi_agent_roles_resolve_to_distinct_prompt_profiles() -> None:
    profiles = load_profiles()

    roles = profiles.MULTI_AGENT_ROLES
    for name in (
        "evidence_researcher",
        "adversarial_reviewer",
        "architecture_reviewer",
        "operations_risk_reviewer",
        "synthesizer",
    ):
        assert name in roles, f"missing multi-agent role: {name}"

    resolved = [profiles.resolve_role(name) for name in roles]
    assert len({item.name for item in resolved}) == len(roles), "roles must not share a profile"


def test_adversarial_and_operations_roles_carry_their_own_review_posture() -> None:
    profiles = load_profiles()

    adversarial = profiles.resolve_role("adversarial_reviewer")
    assert adversarial.challenge_policy == "adversarial"

    architecture = profiles.resolve_role("architecture_reviewer")
    operations = profiles.resolve_role("operations_risk_reviewer")
    assert architecture.action_authority == "read-only"
    assert operations.action_authority == "read-only"
    assert architecture.objective != operations.objective

    with pytest.raises(profiles.PromptProfileError):
        profiles.resolve_role("no_such_role")


# --------------------------------------------------------------------------
# Per-lane timeout
# --------------------------------------------------------------------------


def test_lane_timeout_marks_only_the_slow_lane_and_keeps_sibling_results(tmp_path: Path) -> None:
    module = load_multi()
    release = threading.Event()
    ok_execute = _ok_execute()

    def execute(path: Path, *, dry_run: bool):
        value = json.loads(path.read_text(encoding="utf-8"))
        if str(value.get("lane_id") or path.parent.name).endswith("s1"):
            # Simulate a web session that never returns within the lane budget.
            release.wait(timeout=30)
        return ok_execute(path, dry_run=dry_run)

    try:
        result = module.run_multi(
            make_manifest(tmp_path, 3, lane_timeout_seconds=1),
            execute=execute,
        )
    finally:
        release.set()

    lanes = {item["id"]: item for item in result["lanes"]}
    assert lanes["s1"]["ok"] is False
    assert lanes["s1"]["status"] == "timeout"
    assert lanes["s0"]["ok"] is True
    assert lanes["s2"]["ok"] is True
    assert result["status"] == "partial"
    assert [item["id"] for item in result["lanes"]] == ["s0", "s1", "s2"]


def test_lane_timeout_records_the_abandoned_session_for_cleanup(tmp_path: Path) -> None:
    module = load_multi()
    release = threading.Event()

    def execute(path: Path, *, dry_run: bool):
        release.wait(timeout=30)
        return {"ok": True, "run_dir": None}

    try:
        result = module.run_multi(
            make_manifest(tmp_path, 2, lane_timeout_seconds=1),
            execute=execute,
        )
    finally:
        release.set()

    assert result["ok"] is False
    assert all(item["status"] == "timeout" for item in result["lanes"])
    assert result["abandoned_lane_ids"] == ["s0", "s1"]


# --------------------------------------------------------------------------
# Cancellation
# --------------------------------------------------------------------------


def test_cancellation_skips_pending_waves_and_marks_them_cancelled(tmp_path: Path) -> None:
    module = load_multi()
    cancel = threading.Event()
    ok_execute = _ok_execute()
    started: list[str] = []

    def execute(path: Path, *, dry_run: bool):
        started.append(path.parent.name)
        cancel.set()
        return ok_execute(path, dry_run=dry_run)

    result = module.run_multi(
        make_manifest(tmp_path, 4, max_concurrency=2),
        execute=execute,
        cancel_event=cancel,
    )

    lanes = {item["id"]: item for item in result["lanes"]}
    assert result["status"] == "cancelled"
    assert result["ok"] is False
    assert lanes["s2"]["status"] == "cancelled"
    assert lanes["s3"]["status"] == "cancelled"
    # The merger must not be submitted for a cancelled run.
    assert result.get("merger_run_dir") is None
    assert len(started) == 2


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------


def test_redaction_masks_secrets_in_nested_payloads() -> None:
    redaction = load_redaction()

    payload = {
        "session_locator": "https://chatgpt.com/c/68b0c1de-0000-4000-8000-abcdef123456",
        "headers": {"Cookie": "__Secure-next-auth.session-token=abc123def456ghi789"},
        "token": "sk-proj-AAAABBBBCCCCDDDDEEEEFFFF",
        "lanes": [{"note": "Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature"}],
        "safe": "evidence_researcher",
    }

    masked = redaction.redact_payload(payload)

    flat = json.dumps(masked)
    assert "68b0c1de-0000-4000-8000-abcdef123456" not in flat
    assert "abc123def456ghi789" not in flat
    assert "sk-proj-AAAABBBBCCCCDDDDEEEEFFFF" not in flat
    assert "eyJhbGciOiJIUzI1NiJ9.payload.signature" not in flat
    assert masked["safe"] == "evidence_researcher"
    assert redaction.REDACTED in flat
    # Redaction must not mutate the caller's object.
    assert payload["token"] == "sk-proj-AAAABBBBCCCCDDDDEEEEFFFF"


def test_redaction_preserves_structure_and_is_idempotent() -> None:
    redaction = load_redaction()

    payload = {"token": "sk-proj-AAAABBBBCCCCDDDDEEEEFFFF", "count": 3, "ok": True, "none": None}
    once = redaction.redact_payload(payload)
    twice = redaction.redact_payload(once)

    assert once == twice
    assert once["count"] == 3
    assert once["ok"] is True
    assert once["none"] is None


# --------------------------------------------------------------------------
# Wave visibility in analysis (non-strict) mode
# --------------------------------------------------------------------------


def test_analysis_mode_reports_waves_without_requiring_strict(tmp_path: Path) -> None:
    module = load_multi()

    result = module.run_multi(
        make_manifest(tmp_path, 7, max_concurrency=3),
        execute=_ok_execute(),
    )

    assert [item["lane_ids"] for item in result["waves"]] == [
        ["s0", "s1", "s2"],
        ["s3", "s4", "s5"],
        ["s6"],
    ]


# --------------------------------------------------------------------------
# Role-driven CLI
# --------------------------------------------------------------------------


def test_cli_builds_one_independent_lane_per_requested_role(tmp_path: Path) -> None:
    cli = load_cli()

    plan = cli.build_plan(
        task="Audit the paper-ledger epoch gate.",
        roles=["evidence_researcher", "adversarial_reviewer", "architecture_reviewer"],
        project_root=tmp_path,
        output_dir=tmp_path / "out",
        max_concurrency=3,
    )

    manifest = json.loads(Path(plan["manifest_path"]).read_text(encoding="utf-8"))
    assert [item["id"] for item in manifest["solvers"]] == [
        "evidence_researcher",
        "adversarial_reviewer",
        "architecture_reviewer",
    ]
    assert manifest["max_concurrency"] == 3
    assert all(item["access"] == "read-only" for item in manifest["solvers"])

    missions = [Path(item["mission_path"]).read_text(encoding="utf-8") for item in manifest["solvers"]]
    assert len(set(missions)) == 3, "each role must receive its own mission text"
    assert all("Audit the paper-ledger epoch gate." in text for text in missions)
    assert Path(manifest["merger_mission_path"]).is_file()


def test_cli_run_reports_a_distinct_session_id_per_role(tmp_path: Path) -> None:
    cli = load_cli()

    report = cli.run_plan(
        cli.build_plan(
            task="Audit the sizing path.",
            roles=["evidence_researcher", "adversarial_reviewer"],
            project_root=tmp_path,
            output_dir=tmp_path / "out",
            max_concurrency=2,
        ),
        execute=_ok_execute(),
    )

    sessions = [item["session_locator"] for item in report["workers"]]
    assert len(sessions) == 2
    assert all(sessions), "every worker must report a session identifier"
    assert len(set(sessions)) == 2, "personas inside one session are not multi-agent"
    assert report["independent_session_count"] == 2
    assert [item["role"] for item in report["workers"]] == [
        "evidence_researcher",
        "adversarial_reviewer",
    ]
    assert all(item["duration_seconds"] >= 0 for item in report["workers"])
    assert all(item["artifact_path"] for item in report["workers"])
    assert report["synthesis_path"]


def test_cli_refuses_to_claim_multi_agent_when_sessions_collapse(tmp_path: Path) -> None:
    cli = load_cli()

    def collapsed(path: Path, *, dry_run: bool):
        run_dir = path.parent / "fake-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "output.md").write_text("answer", encoding="utf-8")
        (run_dir / "state.json").write_text(
            json.dumps({"oracle": {"session_locator": "https://chatgpt.com/c/same-session"}}),
            encoding="utf-8",
        )
        return {"ok": True, "run_dir": str(run_dir)}

    report = cli.run_plan(
        cli.build_plan(
            task="Audit anything.",
            roles=["evidence_researcher", "adversarial_reviewer"],
            project_root=tmp_path,
            output_dir=tmp_path / "out",
            max_concurrency=2,
        ),
        execute=collapsed,
    )

    assert report["independent_session_count"] == 1
    assert report["ok"] is False
    assert "SHARED_SESSION" in report["error"]["code"]


def test_cli_dry_run_prints_the_plan_without_executing(tmp_path: Path, capsys) -> None:
    cli = load_cli()
    calls: list[Path] = []

    def execute(path: Path, *, dry_run: bool):
        calls.append(path)
        return {"ok": True, "run_dir": None}

    exit_code = cli.main(
        [
            "run",
            "--mode",
            "analysis",
            "--task",
            "Audit the daily loss gate.",
            "--roles",
            "evidence_researcher,adversarial_reviewer",
            "--max-concurrency",
            "2",
            "--project-root",
            str(tmp_path),
            "--output-dir",
            str(tmp_path / "out"),
            "--plan-only",
        ],
        execute=execute,
    )

    assert exit_code == 0
    assert calls == [], "--plan-only must not submit any web session"
    printed = json.loads(capsys.readouterr().out)
    assert printed["mode"] == "analysis"
    assert [item["role"] for item in printed["workers"]] == [
        "evidence_researcher",
        "adversarial_reviewer",
    ]
    assert printed["waves"] == [{"index": 0, "lane_ids": ["evidence_researcher", "adversarial_reviewer"]}]


def test_cli_rejects_write_roles_sharing_a_worktree(tmp_path: Path) -> None:
    cli = load_cli()

    with pytest.raises(cli.MultiAgentError):
        cli.build_plan(
            task="Implement two things.",
            roles=["implementer_a", "implementer_b"],
            project_root=tmp_path,
            output_dir=tmp_path / "out",
            max_concurrency=2,
            mode="implementation",
            worktrees={"implementer_a": tmp_path / "wt", "implementer_b": tmp_path / "wt"},
        )


# --------------------------------------------------------------------------
# CI collection
# --------------------------------------------------------------------------


def test_multi_agent_suites_are_collected_by_the_fast_gate() -> None:
    gate = (ROOT / "scripts" / "run_fast_gate.py").read_text(encoding="utf-8")

    assert "tests/test_chatgpt_multi_agent.py" in gate
    assert "tests/test_chatgpt_oracle_multi.py" in gate


# --------------------------------------------------------------------------
# Non-ASCII path portability
# --------------------------------------------------------------------------


def test_git_helpers_decode_non_ascii_paths_independently_of_the_console_codepage(
    tmp_path: Path,
) -> None:
    """Git speaks UTF-8; decoding its output with the ANSI codepage corrupts paths.

    On a Korean-locale Windows host every strict worktree lane failed with
    "write worktree is not a Git worktree" because the repository path came back
    mojibake from a cp949 decode.
    """
    import subprocess

    module = load_multi()
    root = tmp_path / "저장소-데모"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)

    common_dir = module._git_common_dir(root)

    assert common_dir.is_dir()
    assert "저장소-데모" in str(common_dir)
    assert module._git(root, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"


# --------------------------------------------------------------------------
# Dry run: prove N independent submissions without spending web sessions
# --------------------------------------------------------------------------


def test_cli_dry_run_builds_one_distinct_child_manifest_per_role(tmp_path: Path) -> None:
    """A dry run must still exercise the real per-lane manifest construction.

    --plan-only stops before the runner, so it cannot show that each role gets
    its own submission. The dry run goes all the way to the runner boundary and
    is the cheapest evidence that N roles produce N separate submissions rather
    than one conversation answering N times.
    """
    cli = load_cli()
    seen: list[dict] = []

    def execute(path: Path, *, dry_run: bool):
        assert dry_run is True, "a dry run must never submit"
        seen.append(json.loads(path.read_text(encoding="utf-8")))
        return {"ok": True, "run_dir": None}

    report = cli.run_plan(
        cli.build_plan(
            task="Audit the sizing path.",
            roles=["evidence_researcher", "adversarial_reviewer", "architecture_reviewer"],
            project_root=tmp_path,
            output_dir=tmp_path / "out",
            max_concurrency=3,
        ),
        execute=execute,
        dry_run=True,
    )

    # Three workers plus the synthesis session, each its own child manifest.
    assert len(seen) == 4
    assert len({item["mission_path"] for item in seen}) == 4
    assert len({str(path) for path in seen}) == 4
    # All children belong to one parent run.
    assert len({item["parallel_parent_id"] for item in seen}) == 1
    assert report["status"] == "dry-run" or report["ok"] is True

    missions = [Path(item["mission_path"]).read_text(encoding="utf-8") for item in seen]
    for role in ("evidence_researcher", "adversarial_reviewer", "architecture_reviewer"):
        assert any(f"You are the {role} worker" in text for text in missions)


def test_cli_dry_run_flag_reaches_the_runner(tmp_path: Path, capsys) -> None:
    cli = load_cli()
    modes: list[bool] = []

    def execute(path: Path, *, dry_run: bool):
        modes.append(dry_run)
        return {"ok": True, "run_dir": None}

    exit_code = cli.main(
        [
            "run", "--mode", "analysis",
            "--task", "Audit the daily loss gate.",
            "--roles", "evidence_researcher,adversarial_reviewer",
            "--max-concurrency", "2",
            "--project-root", str(tmp_path),
            "--output-dir", str(tmp_path / "out"),
            "--dry-run",
        ],
        execute=execute,
    )

    assert exit_code == 0
    assert modes and all(modes), "--dry-run must put every lane in dry-run mode"
    printed = json.loads(capsys.readouterr().out)
    assert printed["submitted"] is False
    assert printed["independent_submission_count"] == 3


# --------------------------------------------------------------------------
# Child provenance must not mislabel a read-only lane as a strict writer child
# --------------------------------------------------------------------------


def test_read_only_child_manifest_does_not_advertise_strict_writer_provenance(
    tmp_path: Path,
) -> None:
    """A read-only lane must not be handed to the runner as a strict writer child.

    `web_multi_devspace_qualification_target` treats any manifest carrying
    `web_multi_child_provenance_path` as a strict worktree-write child and
    requires a v2 parent, `access: worktree-write`, and a worktree under
    `output_dir/worktrees`.  A non-strict read-only lane can satisfy none of
    those, so advertising the field makes the real runner reject every analysis
    lane with WEB_MULTI_DERIVED_ROOT_INVALID.
    """
    module = load_multi()
    config = module.load_manifest(make_manifest(tmp_path, 2))
    lane = config["solvers"][0]

    child = module._child_manifest(config, lane, "p" * 64)
    payload = json.loads(child.read_text(encoding="utf-8"))

    assert payload.get("web_multi_child_provenance_path") is None
    # The provenance file itself is still written - it is the audit record.
    assert (config["output_dir"] / "lanes" / lane["id"] / "child-provenance.json").is_file()


def test_strict_writer_child_manifest_still_advertises_its_provenance(
    tmp_path: Path, monkeypatch
) -> None:
    """The writer binding gate must keep working - this is a security boundary."""
    import subprocess

    module = load_multi()
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "T"], check=True)
    (root / "runtime.txt").write_text("base\n", encoding="utf-8")
    (root / "tests.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "baseline"], check=True)

    output = root / ".workflow" / "ultra"
    output.mkdir(parents=True)
    worktrees = [output / "worktrees" / "runtime", output / "worktrees" / "tests"]
    for worktree in worktrees:
        subprocess.run(
            ["git", "-C", str(root), "worktree", "add", "--detach", str(worktree), "HEAD"],
            check=True, capture_output=True,
        )
    missions = []
    for lane_name in ("runtime", "tests"):
        mission = output / f"{lane_name}.md"
        mission.write_text(f"Implement {lane_name}.", encoding="utf-8")
        missions.append(mission)
    merger = output / "merge.md"
    merger.write_text("Merge.", encoding="utf-8")
    copy_profile = tmp_path / "browser-profile"
    copy_profile.mkdir()

    manifest = output / "multi.json"
    manifest.write_text(json.dumps({
        "schema": "codex.chatgpt.oracle-multi/v2",
        "project_root": str(root.resolve()),
        "output_dir": str(output.resolve()),
        "allowed_worktree_roots": [str(p.resolve()) for p in worktrees],
        "app_name": "codex",
        "model": "gpt-5.6",
        "copy_profile": str(copy_profile.resolve()),
        "max_concurrency": 2,
        "all_lanes_required": True,
        "partial_merge_allowed": False,
        "solvers": [
            {"id": "runtime", "mission_path": str(missions[0]), "project_root": str(worktrees[0]),
             "access": "worktree-write", "owned_paths": ["runtime.txt"]},
            {"id": "tests", "mission_path": str(missions[1]), "project_root": str(worktrees[1]),
             "access": "worktree-write", "owned_paths": ["tests.txt"]},
        ],
        "merger_mission_path": str(merger.resolve()),
        "next_stage_result_path": str((output / "stage-result.json").resolve()),
    }), encoding="utf-8")

    config = module.load_manifest(manifest)
    child = module._child_manifest(config, config["solvers"][0], "q" * 64)
    payload = json.loads(child.read_text(encoding="utf-8"))

    assert payload.get("web_multi_child_provenance_path"), (
        "strict writer children must stay bound to their provenance"
    )


# --------------------------------------------------------------------------
# Real-runner independence smoke
# --------------------------------------------------------------------------


def test_real_runner_builds_one_launch_per_role_and_submits_nothing() -> None:
    """The fake-runner tests cannot rule out the failure this surface exists for.

    A fake reports whatever the caller wants, so it can never show that the real
    Oracle runner accepts each lane and builds a separate launch for it. This
    drives the real runner up to the submission boundary instead - it creates no
    web session and costs nothing, which is why it can live in the gate.
    """
    smoke = _load(
        "multi_agent_smoke_script", ROOT / "scripts" / "run_multi_agent_smoke.py"
    )
    result = smoke.run_smoke(bin_root=ROOT / "bin")

    assert result["failed_checks"] == [], result["failed_checks"]
    assert result["ok"] is True
    assert result["submitted_question"] is False
    assert result["observed_launches"] == result["expected_submissions"] == 4


# --------------------------------------------------------------------------
# Regression tests for the 4 CLI defects
# --------------------------------------------------------------------------


def test_preflight_blocks_when_qualification_fails(tmp_path: Path) -> None:
    cli = load_cli()
    called_runner = False

    def fake_execute(path: Path, *, dry_run: bool):
        nonlocal called_runner
        called_runner = True
        return {"ok": True, "run_dir": None}

    def failing_preflight(root: Path) -> dict[str, Any]:
        raise RuntimeError("root qualification failed")

    plan = cli.build_plan(
        task="Audit preflight block.",
        roles=["evidence_researcher", "adversarial_reviewer"],
        project_root=tmp_path,
        output_dir=tmp_path / "out",
        max_concurrency=2,
    )
    report = cli.run_plan(plan, execute=fake_execute, preflight_verifier=failing_preflight)
    assert report["ok"] is False
    assert report["status"] == "preflight_failed"
    assert report["error"]["code"] == "MULTI_AGENT_PREFLIGHT_FAILED"
    assert "root qualification failed" in report["error"]["message"]
    assert not called_runner, "runner must never be called if preflight fails"


def test_partial_lane_failure_marks_run_not_ok(tmp_path: Path) -> None:
    cli = load_cli()

    def partial_failure(path: Path, *, dry_run: bool):
        payload = json.loads(path.read_text(encoding="utf-8"))
        lane_id = payload.get("mission_path", "")
        run_dir = path.parent / "fake-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        if "evidence_researcher" in str(lane_id):
            (run_dir / "output.md").write_text("good evidence", encoding="utf-8")
            (run_dir / "state.json").write_text(
                json.dumps({"oracle": {"session_locator": "https://chatgpt.com/c/session-1"}}),
                encoding="utf-8",
            )
            return {"ok": True, "run_dir": str(run_dir)}
        else:
            return {"ok": False, "run_dir": str(run_dir)}

    plan = cli.build_plan(
        task="Audit partial failure.",
        roles=["evidence_researcher", "adversarial_reviewer"],
        project_root=tmp_path,
        output_dir=tmp_path / "out",
        max_concurrency=2,
    )
    report = cli.run_plan(plan, execute=partial_failure)
    # 완료 != 성공: 부분 실패는 ok=False여야 함
    assert report["ok"] is False
    assert "adversarial_reviewer" in report["failed_roles"]
    assert report["error"]["code"] == "MULTI_AGENT_LANES_FAILED"


def test_lane_without_output_is_marked_failed(tmp_path: Path) -> None:
    cli = load_cli()

    def empty_output_execute(path: Path, *, dry_run: bool):
        run_dir = path.parent / "fake-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "state.json").write_text(
            json.dumps({"oracle": {"session_locator": "https://chatgpt.com/c/session-1"}}),
            encoding="utf-8",
        )
        return {"ok": True, "run_dir": str(run_dir)}

    plan = cli.build_plan(
        task="Audit empty output.",
        roles=["evidence_researcher", "adversarial_reviewer"],
        project_root=tmp_path,
        output_dir=tmp_path / "out",
        max_concurrency=2,
    )
    report = cli.run_plan(plan, execute=empty_output_execute)
    assert report["ok"] is False
    assert len(report["failed_roles"]) == 2


def test_atomic_replace_retry_on_windows_transient_error(tmp_path: Path, monkeypatch) -> None:
    import os
    preflight = load_cli().PREFLIGHT
    target = tmp_path / "test.json"

    original_replace = os.replace
    attempts = 0

    def flaky_replace(src, dst):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            err = PermissionError("Access is denied")
            err.winerror = 5
            raise err
        return original_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)
    preflight._write_json_atomic(target, {"status": "ok"})

    assert target.is_file()
    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "ok"}
    assert attempts == 3


def test_cli_skip_preflight_flag(tmp_path: Path) -> None:
    cli = load_cli()
    modes = []

    def execute(path: Path, *, dry_run: bool):
        modes.append(dry_run)
        return {"ok": True, "run_dir": None}

    exit_code = cli.main(
        [
            "run", "--mode", "analysis",
            "--task", "Audit skip preflight.",
            "--roles", "evidence_researcher,adversarial_reviewer",
            "--max-concurrency", "2",
            "--project-root", str(tmp_path),
            "--output-dir", str(tmp_path / "out"),
            "--skip-preflight",
            "--dry-run",
        ],
        execute=execute,
    )
    assert exit_code == 0


def test_wave_launch_stagger_spaces_out_concurrent_workers(tmp_path: Path, monkeypatch) -> None:
    import time as time_module

    cli = load_cli()
    sleeps: list[float] = []
    original_sleep = time_module.sleep

    def recording_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def execute(path: Path, *, dry_run: bool):
        payload = json.loads(path.read_text(encoding="utf-8"))
        lane_id = Path(str(payload.get("mission_path"))).stem
        run_dir = path.parent / "fake-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "output.md").write_text("evidence", encoding="utf-8")
        (run_dir / "state.json").write_text(
            json.dumps({"oracle": {"session_locator": f"https://chatgpt.com/c/{lane_id}"}}),
            encoding="utf-8",
        )
        return {"ok": True, "run_dir": str(run_dir)}

    plan = cli.build_plan(
        task="Audit launch stagger.",
        roles=["evidence_researcher", "adversarial_reviewer", "architecture_reviewer"],
        project_root=tmp_path,
        output_dir=tmp_path / "out",
        max_concurrency=3,
    )
    monkeypatch.setattr(time_module, "sleep", recording_sleep)
    try:
        report = cli.run_plan(plan, execute=execute)
    finally:
        monkeypatch.setattr(time_module, "sleep", original_sleep)

    assert report["requested_worker_count"] == 3
    # 첫 워커는 지연 없이 출발하고, 뒤따르는 워커만 launch_index 만큼 벌어진다.
    assert sorted(sleeps) == [0.05, 0.1]
    assert all(delay <= 0.2 for delay in sleeps)


def test_dry_run_wave_does_not_stagger(tmp_path: Path, monkeypatch) -> None:
    import time as time_module

    cli = load_cli()
    sleeps: list[float] = []
    original_sleep = time_module.sleep

    def execute(path: Path, *, dry_run: bool):
        return {"ok": True, "run_dir": None}

    plan = cli.build_plan(
        task="Audit dry-run stagger.",
        roles=["evidence_researcher", "adversarial_reviewer", "architecture_reviewer"],
        project_root=tmp_path,
        output_dir=tmp_path / "out",
        max_concurrency=3,
    )
    monkeypatch.setattr(time_module, "sleep", lambda seconds: sleeps.append(seconds))
    try:
        report = cli.run_plan(plan, execute=execute, dry_run=True)
    finally:
        monkeypatch.setattr(time_module, "sleep", original_sleep)

    assert report["submitted"] is False
    assert sleeps == []
