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
    name = "chatgpt_oracle_run_submission_state_unrecoverable_test"
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
    run_id = "20260905T070422Z-48693a3c751a"
    slug = "oracle-codex-web-gpt-48693a3c75"
    run_dir = run_root / run_id
    run_dir.mkdir(parents=True)
    project_root = tmp_path / "project"
    project_root.mkdir()
    mission = run_dir / "mission.md"
    mission.write_text("verify exact project\n", encoding="utf-8")
    stdout = run_dir / "stdout.log"
    stdout.write_text("ERROR: socket hang up\nUser error (browser-automation): socket hang up\n", encoding="utf-8")
    stderr = run_dir / "stderr.log"
    stderr.write_text("", encoding="utf-8")
    transcript = run_dir / "transcript.md"
    transcript.write_text(stdout.read_text(encoding="utf-8"), encoding="utf-8")
    browser_temp = run_dir / "browser-temp"
    profile = browser_temp / "oracle-browser-test"
    profile.mkdir(parents=True)
    (browser_temp / ".owner.json").write_text("{}\n", encoding="utf-8")
    state_path = run_dir / "state.json"
    mission_sha = runner.STATE.sha256_file(mission)
    project_sha = hashlib.sha256(str(project_root.resolve()).casefold().encode("utf-8")).hexdigest()
    state = {
        "schema": "codex.chatgpt.oracle-run-state/v1",
        "run_id": run_id,
        "project_root": str(project_root),
        "mode": "browser",
        "transport": "devspace",
        "profile": {"copy_profile": str(tmp_path / "source-profile")},
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
        "transport_status": "failed",
        "task_outcome_contract": "v1",
        "task_outcome": "pending",
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
            "expected_cdp_port": 52475,
            "receipt_path": None,
            "receipt_sha256": None,
        },
        "provider_session": {
            "schema": "codex.chatgpt.oracle-provider-session/v1",
            "status": "error",
            "terminal_confirmed": False,
            "binding": "unconfirmed",
            "reason": "browser-identity-receipt-unavailable",
        },
        "status": "attention_required",
        "exit_code": 1,
        "session_authority": "submitted_unknown",
        "terminal_harvested": False,
        "browser_observer": {"status": "process-exited", "oracle_process_pid": 24740},
    }
    runner.STATE.write_json_atomic(state_path, state)
    ownership = runner.STATE.persist_ownership_receipt(state_path, oracle_process_pid=24740)

    session_root = tmp_path / "oracle-sessions"
    meta_dir = session_root / slug
    meta_dir.mkdir(parents=True)
    meta_path = meta_dir / "meta.json"
    meta = {
        "id": slug,
        "status": "error",
        "cwd": str(project_root),
        "mode": "browser",
        "browser": {
            "config": {
                "debugPort": 52475,
                "copyProfileSource": str(tmp_path / "source-profile"),
                "attachRunning": False,
            }
        },
        "options": {
            "slug": slug,
            "mode": "browser",
            "browserConfig": {
                "debugPort": 52475,
                "copyProfileSource": str(tmp_path / "source-profile"),
                "attachRunning": False,
            },
            "writeOutputPath": str(run_dir / "output.md"),
        },
        "errorMessage": "socket hang up",
        "error": {
            "category": "browser-automation",
            "message": "socket hang up",
            "details": {"stage": "execute-browser"},
        },
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    monkeypatch.setenv("ORACLE_SESSION_ROOT", str(session_root))
    recovery_stdout = run_dir / "recovery-harvest-stdout.log"
    recovery_stderr = run_dir / "recovery-harvest-stderr.log"
    recovery_stdout.write_text(
        f'No live ChatGPT tab matched session "{slug}". Attempting recovery by reopening the saved conversation URL.\n',
        encoding="utf-8",
    )
    recovery_stderr.write_text(
        "Cannot recover conversation: session metadata has no recoverable ChatGPT conversation URL "
        "(expected browser.harvest.url or browser.runtime.tabUrl to be a chatgpt.com/c/<id> URL).\n",
        encoding="utf-8",
    )
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
    return runner, run_dir, state_path, meta_path, hashes, profile


def settle(runner, run_dir: Path, hashes: dict[str, str], **kwargs):
    return runner.settle_submission_state_unrecoverable(
        run_dir,
        confirmation=runner.STATE.USER_AUTHORIZED_SUBMISSION_STATE_UNRECOVERABLE,
        reason="submission state is no longer provable from exact historical evidence",
        process_alive=kwargs.pop("process_alive", lambda _pid: False),
        **hashes,
        **kwargs,
    )


def test_exact_b_shape_is_eligible_and_terminalizes_unknown_without_submission_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, run_dir, state_path, _meta_path, hashes, _profile = make_run(tmp_path, monkeypatch)
    dry = settle(runner, run_dir, hashes, dry_run=True)
    assert dry["status"] == "dry-run"
    assert dry["settlement_payload"]["prompt_submission_state"] == "unrecoverable"
    assert dry["settlement_payload"]["task_outcome"] == "unknown"
    assert dry["settlement_payload"]["transport_status"] == "submission_state_unrecoverable"
    assert dry["settlement_payload"]["submission_action"] == "none"
    assert not Path(dry["settlement_path"]).exists()

    settled = settle(runner, run_dir, hashes)
    state = runner.STATE.load_state(state_path)
    proof = runner.STATE.proven_submission_state_unrecoverable(state_path)
    assert settled["safe_for_fresh_run"] is True
    assert state["status"] == "complete"
    assert state["session_authority"] == "terminal"
    assert state["transport_status"] == "submission_state_unrecoverable"
    assert state["task_outcome"] == "unknown"
    assert state["terminal_harvested"] is False
    assert proof is not None
    assert proof["prompt_submission_state"] == "unrecoverable"
    assert "prompt_submitted" not in proof


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("prompt_true", "SUBMISSION_STATE_UNRECOVERABLE_RUNTIME_CONTRADICTION"),
        ("prompt_false", "SUBMISSION_STATE_UNRECOVERABLE_RUNTIME_CONTRADICTION"),
        ("stable_url", "SUBMISSION_STATE_UNRECOVERABLE_STABLE_BINDING_PRESENT"),
        ("conversation_id", "SUBMISSION_STATE_UNRECOVERABLE_STABLE_BINDING_PRESENT"),
        ("output", "SUBMISSION_STATE_UNRECOVERABLE_OUTPUT_PRESENT"),
        ("browser_receipt", "SUBMISSION_STATE_UNRECOVERABLE_BROWSER_RECEIPT_PRESENT"),
        ("wrong_version", "SUBMISSION_STATE_UNRECOVERABLE_ORACLE_VERSION_REQUIRED"),
        ("wrong_category", "SUBMISSION_STATE_UNRECOVERABLE_ERROR_ENVELOPE_REQUIRED"),
        ("wrong_stage", "SUBMISSION_STATE_UNRECOVERABLE_ERROR_ENVELOPE_REQUIRED"),
        ("profile_escape", "SUBMISSION_STATE_UNRECOVERABLE_BROWSER_PROFILE_INVALID"),
        ("port_mismatch", "SUBMISSION_STATE_UNRECOVERABLE_BROWSER_CONFIG_MISMATCH"),
        ("recovery_candidate", "SUBMISSION_STATE_UNRECOVERABLE_RECOVERY_INVALID"),
        ("recovery_resubmit", "SUBMISSION_STATE_UNRECOVERABLE_RECOVERY_INVALID"),
    ],
)
def test_ambiguous_settlement_rejects_contradictory_or_broader_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    code: str,
) -> None:
    runner, run_dir, state_path, meta_path, hashes, profile = make_run(tmp_path, monkeypatch)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if mutation in {"prompt_true", "prompt_false", "stable_url", "conversation_id", "profile_escape"}:
        runtime = meta.setdefault("browser", {}).setdefault("runtime", {})
        runtime.update({
            "chromePort": 52475,
            "chromeTargetId": "target",
            "userDataDir": str(profile),
        })
        if mutation == "prompt_true":
            runtime["promptSubmitted"] = True
        elif mutation == "prompt_false":
            runtime["promptSubmitted"] = False
        elif mutation == "stable_url":
            runtime["tabUrl"] = "https://chatgpt.com/c/stable-id"
        elif mutation == "conversation_id":
            runtime["conversationId"] = "stable-id"
        elif mutation == "profile_escape":
            foreign = tmp_path / "foreign-profile"
            foreign.mkdir()
            runtime["userDataDir"] = str(foreign)
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        hashes["expected_oracle_meta_sha256"] = runner.STATE.sha256_file(meta_path)
    elif mutation == "output":
        (run_dir / "output.md").write_text("assistant output\n", encoding="utf-8")
    elif mutation == "browser_receipt":
        (run_dir / "browser-identity-receipt.json").write_text("{}\n", encoding="utf-8")
    elif mutation == "wrong_version":
        state = runner.STATE.load_state(state_path)
        state["oracle"]["resolved_version"] = "0.17.1"
        runner.STATE.write_json_atomic(state_path, state)
        hashes["expected_state_sha256"] = runner.STATE.sha256_file(state_path)
    elif mutation == "wrong_category":
        meta["error"]["category"] = "transport"
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        hashes["expected_oracle_meta_sha256"] = runner.STATE.sha256_file(meta_path)
    elif mutation == "wrong_stage":
        meta["error"]["details"]["stage"] = "prepare-browser"
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        hashes["expected_oracle_meta_sha256"] = runner.STATE.sha256_file(meta_path)
    elif mutation == "port_mismatch":
        meta["browser"]["config"]["debugPort"] = 52476
        meta["options"]["browserConfig"]["debugPort"] = 52476
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        hashes["expected_oracle_meta_sha256"] = runner.STATE.sha256_file(meta_path)
    elif mutation == "recovery_candidate":
        path = run_dir / "recovery-harvest-stdout.log"
        path.write_text("candidate https://chatgpt.com/c/stable-id\n", encoding="utf-8")
        hashes["expected_recovery_stdout_sha256"] = runner.STATE.sha256_file(path)
    elif mutation == "recovery_resubmit":
        path = run_dir / "recovery-harvest-stdout.log"
        path.write_text("resubmit prompt\n", encoding="utf-8")
        hashes["expected_recovery_stdout_sha256"] = runner.STATE.sha256_file(path)

    with pytest.raises(runner.OracleRunError) as exc:
        settle(runner, run_dir, hashes)
    assert exc.value.code == code


def test_transient_web_url_is_not_stable_but_does_not_create_eligibility_by_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, run_dir, _state_path, meta_path, hashes, _profile = make_run(tmp_path, monkeypatch)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["browser"]["runtime"] = {"tabUrl": "https://chatgpt.com/c/WEB:request-id"}
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    hashes["expected_oracle_meta_sha256"] = runner.STATE.sha256_file(meta_path)
    with pytest.raises(runner.OracleRunError) as exc:
        settle(runner, run_dir, hashes)
    assert exc.value.code == "SUBMISSION_STATE_UNRECOVERABLE_RUNTIME_CONTRADICTION"


def test_requires_explicit_distinct_confirmation_and_stopped_exact_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, run_dir, _state_path, _meta_path, hashes, _profile = make_run(tmp_path, monkeypatch)

    preview = runner.settle_submission_state_unrecoverable(
        run_dir,
        confirmation="",
        reason="preview exact historical eligibility without granting settlement authority",
        process_alive=lambda _pid: False,
        dry_run=True,
        **hashes,
    )
    assert preview["status"] == "dry-run"
    assert preview["settlement_payload"]["confirmation"] is None
    assert preview["required_confirmation"] == runner.STATE.USER_AUTHORIZED_SUBMISSION_STATE_UNRECOVERABLE

    with pytest.raises(runner.OracleRunError) as confirmation:
        runner.settle_submission_state_unrecoverable(
            run_dir,
            confirmation="user-confirmed-no-submission",
            reason="wrong authority",
            process_alive=lambda _pid: False,
            **hashes,
        )
    assert confirmation.value.code == "SUBMISSION_STATE_UNRECOVERABLE_CONFIRMATION_REQUIRED"
    with pytest.raises(runner.OracleRunError) as active:
        settle(runner, run_dir, hashes, process_alive=lambda pid: pid == 24740)
    assert active.value.code == "SUBMISSION_STATE_UNRECOVERABLE_PROCESS_ACTIVE"


def test_receipt_tamper_restores_unresolved_owner_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, run_dir, state_path, _meta_path, hashes, _profile = make_run(tmp_path, monkeypatch)
    project_root = Path(runner.STATE.load_state(state_path)["project_root"])
    settle(runner, run_dir, hashes)
    proof = runner.STATE.proven_submission_state_unrecoverable(state_path)
    assert proof is not None
    receipt = Path(proof["path"])
    receipt.write_text(receipt.read_text(encoding="utf-8") + " ", encoding="utf-8")
    assert runner.STATE.proven_submission_state_unrecoverable(state_path) is None
    owners = runner.STATE.unresolved_project_sessions(run_dir.parent, project_root)
    assert [row["run_id"] for row in owners] == [hashes["expected_run_id"]]
