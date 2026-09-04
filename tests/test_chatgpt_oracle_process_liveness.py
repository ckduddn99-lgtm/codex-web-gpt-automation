from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

RUNNER_PATH = Path(__file__).resolve().parents[1] / "bin" / "chatgpt_oracle_run.py"
RUNNER_TEST_PATH = Path(__file__).resolve().parent / "test_chatgpt_oracle_run.py"


def load_runner():
    name = "chatgpt_oracle_run_process_liveness_test"
    spec = importlib.util.spec_from_file_location(name, RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_runner_test_helpers():
    name = "chatgpt_oracle_run_process_liveness_helpers"
    spec = importlib.util.spec_from_file_location(name, RUNNER_TEST_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_process_is_alive_uses_shared_state_liveness_probe(monkeypatch) -> None:
    runner = load_runner()
    calls: list[int] = []

    def shared_probe(pid: int) -> bool:
        calls.append(pid)
        return False

    monkeypatch.setattr(runner.STATE, "_process_may_be_alive", shared_probe)

    def forbidden_kill(*_args, **_kwargs):
        raise AssertionError("runner must not bypass the shared process liveness probe")

    monkeypatch.setattr(runner.os, "kill", forbidden_kill)

    assert runner.process_is_alive(29428) is False
    assert calls == [29428]


def test_process_is_alive_preserves_fail_closed_true_result(monkeypatch) -> None:
    runner = load_runner()
    monkeypatch.setattr(runner.STATE, "_process_may_be_alive", lambda _pid: True)

    assert runner.process_is_alive(4242) is True


def test_legacy_selector_evidence_survives_cleaned_run_local_browser_profile(
    tmp_path, monkeypatch
) -> None:
    helpers = load_runner_test_helpers()
    runner = helpers.load_runner()
    helpers.isolated_default_oracle_profile(tmp_path, monkeypatch)
    session_root = tmp_path / "oracle-sessions"
    monkeypatch.setenv("ORACLE_SESSION_ROOT", str(session_root))
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)

    failed = helpers.execute_run(
        runner,
        helpers.manifest(
            tmp_path,
            model="gpt-5.6",
            model_strategy="select",
            thinking_time="extra-high",
            research="off",
            parallel_parent_id="a" * 64,
        ),
        run_factory=helpers.version_0171_runner,
        popen_factory=helpers.direct_model_selector_button_pre_submit_popen(session_root),
    )
    run_dir = Path(failed["run_dir"])
    state_path = run_dir / "state.json"
    state = runner.STATE.load_state(state_path)
    slug = state["oracle"]["slug"]
    meta = runner.STATE.json.loads(
        (session_root / slug / "meta.json").read_text(encoding="utf-8")
    )
    runtime_profile = Path(meta["browser"]["runtime"]["userDataDir"])
    assert runtime_profile.is_dir()
    shutil.rmtree(runtime_profile)
    assert not runtime_profile.exists()

    (run_dir / "recovery-harvest-stdout.log").write_text(
        f'No live ChatGPT tab matched session "{slug}". Attempting recovery.\n',
        encoding="utf-8",
    )
    (run_dir / "recovery-harvest-stderr.log").write_text(
        "Cannot recover conversation: session metadata has no recoverable ChatGPT conversation URL.\n",
        encoding="utf-8",
    )

    evidence = runner.STATE.legacy_unbound_direct_devspace_selector_no_submission_evidence(
        state_path
    )

    assert evidence is not None
    assert evidence["pre_submit_marker"] == "oracle-model-selector-button-missing/v1"
    assert evidence["browser_profile"] == str(runtime_profile.resolve())
