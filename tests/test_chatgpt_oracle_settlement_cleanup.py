from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

RUN_TEST_PATH = Path(__file__).resolve().parent / "test_chatgpt_oracle_run.py"


def load_run_test_module():
    name = "chatgpt_oracle_run_test_helpers_for_cleanup"
    spec = importlib.util.spec_from_file_location(name, RUN_TEST_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_model_option_settlement_revalidates_after_owned_browser_temp_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helpers = load_run_test_module()
    runner = helpers.load_runner()
    monkeypatch.setenv("CODEX_THREAD_ID", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    helpers.isolated_default_oracle_profile(tmp_path, monkeypatch)
    session_root = tmp_path / "oracle-sessions"
    monkeypatch.setenv("ORACLE_SESSION_ROOT", str(session_root))

    failed = helpers.execute_run(
        runner,
        helpers.manifest(
            tmp_path,
            model="gpt-5.5-instant",
            model_strategy="select",
            thinking_time="light",
            research="off",
        ),
        run_factory=helpers.version_0171_runner,
        popen_factory=helpers.model_option_missing_pre_submit_popen(session_root),
    )
    run_dir = Path(failed["run_dir"])
    state_path = run_dir / "state.json"
    recovered = runner.recover_run(
        run_dir,
        action="harvest",
        oracle_command=["oracle"],
        popen_factory=helpers.recovery_binding_unavailable_popen,
    )
    assert recovered["status"] == "recovery_binding_unavailable"
    cleaned = runner.cleanup_prior_boot_browser_temps_preserving_evidence(
        run_dir.parent,
        current_uptime_ms=0,
    )
    assert cleaned == [str(run_dir / "browser-temp")]
    assert (run_dir / "browser-temp").is_dir()
    assert list((run_dir / "browser-temp").iterdir()) == []

    settled = runner.settle_user_confirmed_no_submission(
        run_dir,
        confirmation=runner.STATE.USER_CONFIRMED_NO_SUBMISSION,
        reason="user confirmed the exact pre-submit model failure never submitted",
    )

    assert settled["safe_for_fresh_run"] is True
    assert runner.STATE.proven_user_confirmed_no_submission(state_path) is not None
    assert runner.STATE.unresolved_project_sessions(run_dir.parent, tmp_path) == []


def test_settle_repairs_already_cleaned_canonical_browser_temp_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helpers = load_run_test_module()
    runner = helpers.load_runner()
    monkeypatch.setenv("CODEX_THREAD_ID", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    helpers.isolated_default_oracle_profile(tmp_path, monkeypatch)
    session_root = tmp_path / "oracle-sessions"
    monkeypatch.setenv("ORACLE_SESSION_ROOT", str(session_root))

    failed = helpers.execute_run(
        runner,
        helpers.manifest(
            tmp_path,
            model="gpt-5.5-instant",
            model_strategy="select",
            thinking_time="light",
            research="off",
        ),
        run_factory=helpers.version_0171_runner,
        popen_factory=helpers.model_option_missing_pre_submit_popen(session_root),
    )
    run_dir = Path(failed["run_dir"])
    state_path = run_dir / "state.json"
    recovered = runner.recover_run(
        run_dir,
        action="harvest",
        oracle_command=["oracle"],
        popen_factory=helpers.recovery_binding_unavailable_popen,
    )
    assert recovered["status"] == "recovery_binding_unavailable"
    first_settlement = runner.settle_user_confirmed_no_submission(
        run_dir,
        confirmation=runner.STATE.USER_CONFIRMED_NO_SUBMISSION,
        reason="user confirmed the exact pre-submit model failure never submitted",
    )
    assert first_settlement["safe_for_fresh_run"] is True
    assert runner.STATE.proven_user_confirmed_no_submission(state_path) is not None

    assert runner.STATE.cleanup_owned_browser_temp(run_dir / "browser-temp") is True
    assert not (run_dir / "browser-temp").exists()
    assert runner.STATE.proven_user_confirmed_no_submission(state_path) is None

    settled = runner.settle_user_confirmed_no_submission(
        run_dir,
        confirmation=runner.STATE.USER_CONFIRMED_NO_SUBMISSION,
        reason="user confirmed the exact pre-submit model failure never submitted",
    )

    assert (run_dir / "browser-temp").is_dir()
    assert list((run_dir / "browser-temp").iterdir()) == []
    assert settled["safe_for_fresh_run"] is True
    assert runner.STATE.proven_user_confirmed_no_submission(state_path) is not None
    assert runner.STATE.unresolved_project_sessions(run_dir.parent, tmp_path) == []


def test_cleanup_preserver_rejects_noncanonical_cleaned_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helpers = load_run_test_module()
    runner = helpers.load_runner()
    run_root = tmp_path / "runs"
    run_root.mkdir()
    foreign = tmp_path / "foreign" / "browser-temp"
    monkeypatch.setattr(
        runner.STATE,
        "cleanup_prior_boot_browser_temps",
        lambda *_args, **_kwargs: [str(foreign)],
    )

    with pytest.raises(runner.OracleRunError) as exc:
        runner.cleanup_prior_boot_browser_temps_preserving_evidence(run_root)

    assert exc.value.code == "BROWSER_TEMP_CLEANUP_PATH_INVALID"
    assert not foreign.exists()


def test_settlement_repair_refuses_noncanonical_browser_temp(tmp_path: Path) -> None:
    helpers = load_run_test_module()
    runner = helpers.load_runner()
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    state_path = run_dir / "state.json"
    foreign = tmp_path / "foreign-browser-temp"
    runner.STATE.write_json_atomic(
        state_path,
        {
            "schema": "codex.chatgpt.oracle-run-state/v1",
            "session_authority": "pre_submit",
            "transport_status": "not_submitted_user_confirmed",
            "user_confirmed_no_submission": {"schema": "reference"},
            "artifacts": {"browser_temp": str(foreign)},
        },
    )

    assert runner.restore_cleaned_browser_temp_container_for_settlement(state_path) is False
    assert not foreign.exists()
