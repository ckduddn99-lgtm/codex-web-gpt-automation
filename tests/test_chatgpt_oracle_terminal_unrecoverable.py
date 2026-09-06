from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUN_PATH = ROOT / "bin" / "chatgpt_oracle_run.py"


def load_runner():
    name = "chatgpt_oracle_run_terminal_unrecoverable_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, RUN_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def make_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runner = load_runner()
    run_root = tmp_path / "state" / "projects" / "project-key" / "runs"
    run_id = "20260904T220025Z-f2356278790f"
    slug = "oracle-codex-web-gpt-f235627879"
    run_dir = run_root / run_id
    run_dir.mkdir(parents=True)
    project_root = tmp_path / "project"
    project_root.mkdir()
    mission = run_dir / "mission.md"
    mission.write_text("verify the exact project\n", encoding="utf-8")
    stdout = run_dir / "stdout.log"
    stdout.write_text("oracle browser launched\n", encoding="utf-8")
    stderr = run_dir / "stderr.log"
    stderr.write_text("", encoding="utf-8")
    transcript = run_dir / "transcript.md"
    transcript.write_text("oracle browser launched\n", encoding="utf-8")
    browser_temp = run_dir / "browser-temp"
    profile = browser_temp / "oracle-browser-test"
    profile.mkdir(parents=True)
    state_path = run_dir / "state.json"
    mission_sha = runner.STATE.sha256_file(mission)
    project_sha = hashlib.sha256(str(project_root.resolve()).casefold().encode("utf-8")).hexdigest()
    state = {
        "schema": "codex.chatgpt.oracle-run-state/v1",
        "run_id": run_id,
        "project_root": str(project_root),
        "mode": "browser",
        "transport": "devspace",
        "originating_task": {
            "schema": "codex.chatgpt.oracle-task-owner/v1",
            "source_thread_id": None,
            "binding": "legacy-unbound",
        },
        "ownership": {
            "schema": "codex.chatgpt.oracle-ownership/v1",
            "source_thread_id": None,
            "binding": "legacy-unbound",
            "project_root_sha256": project_sha,
            "run_id": run_id,
            "mission_sha256": mission_sha,
            "slug": slug,
        },
        "transport_status": "incomplete",
        "task_outcome_contract": "v1",
        "task_outcome": "pending",
        "task_outcome_reason": "pending",
        "mission": {
            "path": str(project_root / "mission-source.md"),
            "transport_path": str(mission),
            "sha256": mission_sha,
        },
        "oracle": {
            "resolved_version": "0.18.0",
            "slug": slug,
            "session_locator": slug,
        },
        "artifacts": {
            "output": str(run_dir / "output.md"),
            "transcript": str(transcript),
            "stdout": str(stdout),
            "stderr": str(stderr),
            "browser_temp": str(browser_temp),
        },
        "browser_identity": {
            "schema": "codex.chatgpt.oracle-browser-identity/v1",
            "expected_cdp_port": 53619,
            "receipt_path": None,
            "receipt_sha256": None,
        },
        "provider_session": {
            "schema": "codex.chatgpt.oracle-provider-session/v1",
            "status": "unobserved",
            "terminal_confirmed": False,
            "binding": "none",
            "reason": "oracle-runtime-not-yet-observed",
        },
        "status": "attention_required",
        "exit_code": 1,
        "session_authority": "submitted_unknown",
        "terminal_harvested": False,
        "artifact_sha256": None,
        "browser_observer": {
            "status": "running",
            "timeout_seconds": 6000.0,
            "timeout_is_terminal": False,
            "oracle_process_pid": 2176,
        },
    }
    runner.STATE.write_json_atomic(state_path, state)
    ownership = runner.STATE.persist_ownership_receipt(state_path, oracle_process_pid=2176)
    session_root = tmp_path / "oracle-sessions"
    meta_dir = session_root / slug
    meta_dir.mkdir(parents=True)
    meta_path = meta_dir / "meta.json"
    meta = {
        "status": "running",
        "browser": {
            "runtime": {
                "promptSubmitted": True,
                "tabUrl": "https://chatgpt.com/",
                "conversationId": None,
                "chromePort": 53619,
                "chromeTargetId": "3C4C43B5B1A7FC97310A7F118051D674",
                "userDataDir": str(profile),
                "chromePid": 27028,
                "controllerPid": 7492,
            }
        },
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    monkeypatch.setenv("ORACLE_SESSION_ROOT", str(session_root))
    recovery_stdout = run_dir / "recovery-harvest-stdout.log"
    recovery_stderr = run_dir / "recovery-harvest-stderr.log"
    recovery_stdout.write_text("oracle 0.18.0\n", encoding="utf-8")
    recovery_stderr.write_text("connect ECONNREFUSED 127.0.0.1:53619\n", encoding="utf-8")
    hashes = {
        "expected_run_id": run_id,
        "expected_slug": slug,
        "expected_state_sha256": runner.STATE.sha256_file(state_path),
        "expected_mission_sha256": mission_sha,
        "expected_ownership_receipt_sha256": ownership["sha256"],
        "expected_oracle_meta_sha256": runner.STATE.sha256_file(meta_path),
        "expected_recovery_stdout_sha256": runner.STATE.sha256_file(recovery_stdout),
        "expected_recovery_stderr_sha256": runner.STATE.sha256_file(recovery_stderr),
    }
    return runner, run_dir, state_path, meta_path, hashes


def settle(runner, run_dir: Path, hashes: dict[str, str], **kwargs):
    return runner.settle_submitted_outcome_unrecoverable(
        run_dir,
        confirmation="user-authorized-terminal-unrecoverable",
        reason="exact submitted run permanently lost stable conversation binding",
        process_alive=kwargs.pop("process_alive", lambda _pid: False),
        **hashes,
        **kwargs,
    )


def test_submitted_outcome_unrecoverable_is_append_only_hash_bound_and_releases_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, run_dir, state_path, _meta_path, hashes = make_run(tmp_path, monkeypatch)
    project_root = Path(runner.STATE.load_state(state_path)["project_root"])

    dry = settle(runner, run_dir, hashes, dry_run=True)
    assert dry["status"] == "dry-run"
    assert dry["safe_for_fresh_run"] is True
    assert dry["settlement_payload"]["prompt_submitted"] is True
    assert dry["settlement_payload"]["task_outcome"] == "unknown"
    assert not Path(dry["settlement_path"]).exists()

    settled = settle(runner, run_dir, hashes)
    state = runner.STATE.load_state(state_path)
    proof = runner.STATE.proven_submitted_outcome_unrecoverable(state_path)

    assert settled["safe_for_fresh_run"] is True
    assert state["status"] == "complete"
    assert state["session_authority"] == "terminal"
    assert state["transport_status"] == "submitted_unrecoverable"
    assert state["task_outcome"] == "unknown"
    assert state["terminal_harvested"] is False
    assert proof is not None
    assert proof["prompt_submitted"] is True
    assert runner.STATE.unresolved_project_sessions(run_dir.parent, project_root) == []

    receipt = Path(proof["path"])
    receipt.write_text(receipt.read_text(encoding="utf-8") + " ", encoding="utf-8")
    assert runner.STATE.proven_submitted_outcome_unrecoverable(state_path) is None
    owners = runner.STATE.unresolved_project_sessions(run_dir.parent, project_root)
    assert [row["run_id"] for row in owners] == [hashes["expected_run_id"]]


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("prompt_false", "TERMINAL_UNRECOVERABLE_PROMPT_SUBMISSION_REQUIRED"),
        ("prompt_missing", "TERMINAL_UNRECOVERABLE_PROMPT_SUBMISSION_REQUIRED"),
        ("stable_url", "TERMINAL_UNRECOVERABLE_STABLE_BINDING_PRESENT"),
        ("transient_web_url", "TERMINAL_UNRECOVERABLE_STABLE_BINDING_PRESENT"),
        ("output", "TERMINAL_UNRECOVERABLE_OUTPUT_PRESENT"),
        ("browser_receipt", "TERMINAL_UNRECOVERABLE_BROWSER_RECEIPT_PRESENT"),
        ("meta_tamper", "TERMINAL_UNRECOVERABLE_HASH_MISMATCH"),
        ("recovery_tamper", "TERMINAL_UNRECOVERABLE_HASH_MISMATCH"),
        ("profile_escape", "TERMINAL_UNRECOVERABLE_BROWSER_PROFILE_INVALID"),
        ("port_mismatch", "TERMINAL_UNRECOVERABLE_BROWSER_IDENTITY_MISMATCH"),
        ("target_missing", "TERMINAL_UNRECOVERABLE_BROWSER_IDENTITY_MISMATCH"),
        ("recovery_prompt", "TERMINAL_UNRECOVERABLE_RECOVERY_INVALID"),
        ("recovery_urls", "TERMINAL_UNRECOVERABLE_RECOVERY_INVALID"),
        ("ownership_tamper", "TERMINAL_UNRECOVERABLE_HASH_MISMATCH"),
    ],
)
def test_terminal_unrecoverable_rejects_contradictory_or_tampered_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    code: str,
) -> None:
    runner, run_dir, state_path, meta_path, hashes = make_run(tmp_path, monkeypatch)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if mutation == "prompt_false":
        meta["browser"]["runtime"]["promptSubmitted"] = False
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        hashes["expected_oracle_meta_sha256"] = runner.STATE.sha256_file(meta_path)
    elif mutation == "prompt_missing":
        meta["browser"]["runtime"].pop("promptSubmitted")
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        hashes["expected_oracle_meta_sha256"] = runner.STATE.sha256_file(meta_path)
    elif mutation == "stable_url":
        meta["browser"]["runtime"]["tabUrl"] = "https://chatgpt.com/c/stable-conversation"
        meta["browser"]["runtime"]["conversationId"] = "stable-conversation"
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        hashes["expected_oracle_meta_sha256"] = runner.STATE.sha256_file(meta_path)
    elif mutation == "transient_web_url":
        meta["browser"]["runtime"]["tabUrl"] = "https://chatgpt.com/c/WEB:request-id"
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        hashes["expected_oracle_meta_sha256"] = runner.STATE.sha256_file(meta_path)
    elif mutation == "output":
        (run_dir / "output.md").write_text("assistant answer\n", encoding="utf-8")
    elif mutation == "browser_receipt":
        (run_dir / "browser-identity-receipt.json").write_text("{}\n", encoding="utf-8")
    elif mutation == "meta_tamper":
        meta_path.write_text(meta_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    elif mutation == "recovery_tamper":
        with (run_dir / "recovery-harvest-stderr.log").open("a", encoding="utf-8") as handle:
            handle.write("changed\n")
    elif mutation == "profile_escape":
        foreign = tmp_path / "foreign-profile"
        foreign.mkdir()
        meta["browser"]["runtime"]["userDataDir"] = str(foreign)
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        hashes["expected_oracle_meta_sha256"] = runner.STATE.sha256_file(meta_path)
    elif mutation == "port_mismatch":
        meta["browser"]["runtime"]["chromePort"] = 53620
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        hashes["expected_oracle_meta_sha256"] = runner.STATE.sha256_file(meta_path)
    elif mutation == "target_missing":
        meta["browser"]["runtime"]["chromeTargetId"] = ""
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        hashes["expected_oracle_meta_sha256"] = runner.STATE.sha256_file(meta_path)
    elif mutation == "recovery_prompt":
        recovery = run_dir / "recovery-harvest-stdout.log"
        recovery.write_text("oracle 0.18.0\nsending prompt\n", encoding="utf-8")
        hashes["expected_recovery_stdout_sha256"] = runner.STATE.sha256_file(recovery)
    elif mutation == "recovery_urls":
        recovery = run_dir / "recovery-harvest-stdout.log"
        recovery.write_text(
            "https://chatgpt.com/c/one\nhttps://chatgpt.com/c/two\n", encoding="utf-8"
        )
        hashes["expected_recovery_stdout_sha256"] = runner.STATE.sha256_file(recovery)
    elif mutation == "ownership_tamper":
        ownership = run_dir / "ownership-receipt.json"
        ownership.write_text(ownership.read_text(encoding="utf-8") + " ", encoding="utf-8")

    with pytest.raises(runner.OracleRunError) as exc:
        settle(runner, run_dir, hashes)
    assert exc.value.code == code


def test_terminal_unrecoverable_rejects_live_or_ambiguous_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, run_dir, _state_path, _meta_path, hashes = make_run(tmp_path, monkeypatch)
    with pytest.raises(runner.OracleRunError) as exc:
        settle(runner, run_dir, hashes, process_alive=lambda pid: pid == 27028)
    assert exc.value.code == "TERMINAL_UNRECOVERABLE_PROCESS_ACTIVE"


def test_terminal_unrecoverable_requires_exact_identity_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, run_dir, _state_path, _meta_path, hashes = make_run(tmp_path, monkeypatch)
    hashes["expected_slug"] = "oracle-wrong"
    with pytest.raises(runner.OracleRunError) as exc:
        settle(runner, run_dir, hashes)
    assert exc.value.code == "TERMINAL_UNRECOVERABLE_IDENTITY_MISMATCH"
