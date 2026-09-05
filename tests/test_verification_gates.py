from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_fast_gate_targets_exist_and_cover_the_pre_submit_contracts() -> None:
    gate = load("fast_gate_test", SCRIPTS / "run_fast_gate.py")

    for target in gate.FAST_TARGETS:
        assert (ROOT / target.split("::", 1)[0]).is_file(), target

    covered = {target.split("::", 1)[0] for target in gate.FAST_TARGETS}
    # The buckets that actually blocked runs before submission must be gated.
    assert "tests/test_chatgpt_oracle_state.py" in covered
    assert "tests/test_chatgpt_oracle_run.py" in covered
    assert "tests/test_chatgpt_oracle_compat.py" in covered
    assert "tests/test_chatgpt_oracle_incident.py" in covered
    assert "tests/test_chatgpt_oracle_diagnose.py" in covered
    selected_runner_contracts = {
        target for target in gate.FAST_TARGETS
        if target.startswith("tests/test_chatgpt_oracle_run.py::")
    }
    assert len(selected_runner_contracts) >= 20
    assert any("fresh_app_read_gate" in target for target in selected_runner_contracts)
    assert any("dynamic_cdp_port" in target for target in selected_runner_contracts)
    assert any("foreign_task_recovery" in target for target in selected_runner_contracts)
    assert "tests/test_chatgpt_oracle_run.py" not in gate.FAST_TARGETS
    assert gate.FAST_DESELECTS == [
        "tests/test_chatgpt_oracle_compat.py::test_archived_parent_direct_restore_requires_exact_control_and_composer_transition"
    ]
    deselected_path = gate.FAST_DESELECTS[0].split("::", 1)[0]
    assert (ROOT / deselected_path).is_file()
    # The gate must stay fast enough to run after every batch of edits. Pin an
    # upper bound instead of one exact value so added coverage does not require
    # editing this contract, while a runaway budget still fails.
    assert 30.0 <= gate.DEFAULT_BUDGET_SECONDS <= 120.0


def test_fast_gate_is_a_strict_subset_of_the_full_suite() -> None:
    gate = load("fast_gate_subset_test", SCRIPTS / "run_fast_gate.py")
    all_tests = {
        f"tests/{path.name}" for path in (ROOT / "tests").glob("test_*.py")
    }

    covered = {target.split("::", 1)[0] for target in gate.FAST_TARGETS}
    assert covered < all_tests


def test_fast_gate_hides_windows_console_windows() -> None:
    gate = load("fast_gate_window_test", SCRIPTS / "run_fast_gate.py")

    source = (SCRIPTS / "run_fast_gate.py").read_text(encoding="utf-8")
    assert "CREATE_NO_WINDOW" in source
    assert "SW_HIDE" in source
    assert callable(gate._hidden_process_kwargs)


def test_fast_gate_prioritizes_measured_long_whole_file_jobs() -> None:
    gate = load("fast_gate_priority_test", SCRIPTS / "run_fast_gate.py")

    jobs = gate._group_fast_targets()
    first_paths = [job[0].split("::", 1)[0] for job in jobs[:len(gate.LONG_JOB_PRIORITY)]]

    assert first_paths == list(gate.LONG_JOB_PRIORITY)
    assert gate.DEFAULT_WORKERS <= 3


def test_fast_gate_explicitly_preserves_hidden_child_output(monkeypatch) -> None:
    gate = load("fast_gate_output_test", SCRIPTS / "run_fast_gate.py")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return gate.subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(gate.subprocess, "run", fake_run)
    result = gate.run_fast_gate()

    assert result["exit_code"] == 0
    jobs = gate._group_fast_targets()
    assert len(calls) == len(jobs) == result["jobs"]
    assert result["workers"] == min(gate.DEFAULT_WORKERS, len(jobs))
    # CREATE_NO_WINDOW does not reliably retain implicit standard handles.
    # Bind both streams explicitly so CI/file-backed callers receive pytest's
    # summary, warnings, failures, and progress instead of just the wrapper line.
    assert all(kwargs.get("stdout") is sys.stdout for _, kwargs in calls)
    assert all(kwargs.get("stderr") is sys.stderr for _, kwargs in calls)
    basetemps = [command[command.index("--basetemp") + 1] for command, _ in calls]
    assert len(set(basetemps)) == len(jobs)
    runner_targets = [target for target in gate.FAST_TARGETS
                      if target.startswith("tests/test_chatgpt_oracle_run.py::")]
    runner_calls = [command for command, _ in calls if any(
        target.startswith("tests/test_chatgpt_oracle_run.py::") for target in command)]
    assert len(runner_targets) >= 20
    assert len(runner_calls) == (len(runner_targets) + gate.NODE_TARGETS_PER_JOB - 1) // gate.NODE_TARGETS_PER_JOB
    assert all(1 <= sum(target.startswith("tests/test_chatgpt_oracle_run.py::") for target in command)
               <= gate.NODE_TARGETS_PER_JOB for command in runner_calls)
    assert sum(sum(target.startswith("tests/test_chatgpt_oracle_run.py::") for target in command)
               for command in runner_calls) == len(runner_targets)


def test_fast_gate_wall_clock_includes_temporary_directory_cleanup(monkeypatch) -> None:
    gate = load("fast_gate_cleanup_budget_test", SCRIPTS / "run_fast_gate.py")
    clock = [0.0]

    class SlowCleanup:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return "synthetic-gate-temp"

        def __exit__(self, *args):
            clock[0] += 12.0

    def fake_run(command, **kwargs):
        clock[0] += 5.0
        return gate.subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(gate.tempfile, "TemporaryDirectory", SlowCleanup)
    monkeypatch.setattr(gate.subprocess, "run", fake_run)
    monkeypatch.setattr(gate.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(gate, "_group_fast_targets", lambda: [[gate.FAST_TARGETS[0]]])

    result = gate.run_fast_gate(budget_seconds=10.0, workers=1)

    assert result["exit_code"] == 0
    assert result["elapsed_seconds"] == 17.0
    assert result["within_budget"] is False


def test_golden_path_smoke_passes_against_the_source_tree() -> None:
    smoke = load("golden_path_smoke_test", SCRIPTS / "run_golden_path_smoke.py")

    result = smoke.run_smoke(bin_root=ROOT / "bin")

    assert result["ok"] is True, result["failed_checks"]
    assert result["submitted_question"] is False
    names = [item["check"] for item in result["checks"]]
    for required in (
        "mode_contract_compiles",
        "manifest_loads",
        "devspace_transport_selected",
        "prompt_is_one_line_with_app_mention",
        "dry_run_preview_ok",
        "argv_never_submits_files",
        "argv_hides_browser_window",
        "argv_selects_a_model",
        "profile_copy_matches_host_capability",
        "lifecycle_vocabulary_is_bounded",
    ):
        assert required in names


def test_golden_path_smoke_never_submits_or_launches_a_browser() -> None:
    source = (SCRIPTS / "run_golden_path_smoke.py").read_text(encoding="utf-8")

    assert "dry_run=True" in source
    assert "dry_run=False" not in source
    assert '"submitted_question": False' in source


def test_ci_workflow_runs_the_fast_gate_and_golden_path_smoke() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release-portability.yml").read_text(encoding="utf-8")

    assert "scripts/run_fast_gate.py" in workflow
    assert "scripts/run_golden_path_smoke.py" in workflow
    # The fast gate must run before the long suite so a broken launch contract
    # fails in seconds instead of minutes.
    assert workflow.index("run_fast_gate.py") < workflow.index("run_v4_contract_tests.py --full")


def test_release_manifest_ships_the_new_verification_scripts() -> None:
    manifest = json.loads((ROOT / "install-manifest.json").read_text(encoding="utf-8"))
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))

    assert "bin/chatgpt_oracle_incident.py" in manifest["include"]
    assert "bin/chatgpt_oracle_incident.py" in package["files"]
