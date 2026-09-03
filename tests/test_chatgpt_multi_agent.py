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
