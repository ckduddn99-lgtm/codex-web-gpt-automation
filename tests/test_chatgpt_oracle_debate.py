"""Debate transport tests. All provider responses here are synthetic, not live GPTs."""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from test_chatgpt_oracle_multi import load, make_debate_manifest, _debate_fake_result

ROOT = Path(__file__).resolve().parents[1]


def test_debate_runtime_and_regression_are_registered():
    manifest = json.loads((ROOT / "install-manifest.json").read_text(encoding="utf-8"))
    assert manifest["include"].count("bin/chatgpt_oracle_debate.py") == 1
    tree = ast.parse((ROOT / "scripts/run_fast_gate.py").read_text(encoding="utf-8"))
    targets = next(ast.literal_eval(node.value) for node in tree.body
                   if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "FAST_TARGETS"
                           for target in node.targets))
    assert targets.count("tests/test_chatgpt_oracle_debate.py") == 1


def load_cli():
    spec = importlib.util.spec_from_file_location("debate_cli_test", ROOT / "bin/chatgpt_multi_agent.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def fake_provider(calls, *, verdict="CONSENSUS", mutate=None):
    def execute(path, *, dry_run):
        value = json.loads(path.read_text(encoding="utf-8"))
        name = path.parent.name
        calls.append((name, value, dry_run))
        if dry_run:
            return {"ok": True, "run_dir": None}
        answer = (
            f"Evidence-based assessment.\nDEBATE_VERDICT: {verdict}\n"
            if name.startswith("judge-") else f"Solution and evidence from {name}."
        )
        result = _debate_fake_result(path, value, answer, f"conversation-{name}")
        if mutate is not None:
            mutate(name, path, value, result)
        return result
    return execute


def test_cli_debate_plan_and_dry_run_use_oracle_and_report_only_planned_budget(tmp_path, capsys):
    cli = load_cli()
    calls = []
    code = cli.main([
        "run", "--mode", "debate", "--task", "Resolve competing root-cause hypotheses.",
        "--project-root", str(tmp_path), "--output-dir", str(tmp_path / "out"),
        "--roles", "evidence_researcher,adversarial_reviewer,architecture_reviewer",
        "--debate-rounds", "2", "--dry-run",
    ], execute=fake_provider(calls))
    printed = json.loads(capsys.readouterr().out)
    assert code == 0, printed
    assert printed["mode"] == "debate"
    assert printed["submitted"] is False
    assert printed["consensus_reached"] is None
    assert printed["planned_submission_upper_bound"] == 12
    assert printed["independent_submission_count"] == 0
    assert len(calls) == 12
    assert all(dry for _, _, dry in calls)
    assert all(value["task_outcome_contract"] == "v1" for _, value, _ in calls)
    assert all(value["model"] == "gpt-5.6" and value["model_strategy"] == "select" for _, value, _ in calls)
    assert not (tmp_path / "out/debate-ledger.json").exists()
    assert not (tmp_path / "out/result.json").exists()


def test_cli_plan_only_never_reaches_provider(tmp_path, capsys):
    cli = load_cli()
    calls = []
    code = cli.main([
        "run", "--mode", "debate", "--task", "Review safely.",
        "--project-root", str(tmp_path), "--output-dir", str(tmp_path / "out"), "--plan-only",
    ], execute=fake_provider(calls))
    report = json.loads(capsys.readouterr().out)
    assert code == 0, report
    assert calls == []
    assert report["requested_worker_count"] == 3
    assert report["planned_submission_upper_bound"] == 12
    assert report["submitted"] is False


def _snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("change", ["disabled", "removed", "model", "rounds", "mode"])
def test_changed_debate_plan_is_rejected_before_execution(tmp_path, change):
    cli = load_cli()
    plan = cli.build_plan(task="Review safely", roles=list(cli.DEFAULT_DEBATE_ROLES),
                          project_root=tmp_path, output_dir=tmp_path / "out", mode="debate")
    manifest = Path(plan["manifest_path"])
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if change == "disabled":
        data["debate"]["enabled"] = False
    elif change == "removed":
        data.pop("debate")
    elif change == "model":
        data["model"] = "gpt-5.6-sol"
    elif change == "rounds":
        data["debate"]["max_rounds"] = 3
    else:
        plan["mode"] = "analysis"
    if change != "mode":
        manifest.write_text(json.dumps(data), encoding="utf-8")
    before = _snapshot(tmp_path)
    calls = []
    with pytest.raises(Exception, match="DEBATE_PLAN_CHANGED"):
        cli.run_plan(plan, execute=fake_provider(calls))
    assert not calls
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("relative", [".git/debate", ".GiT/debate", "."])
def test_git_metadata_cannot_be_used_as_debate_output(tmp_path, relative):
    engine = load()
    manifest = make_debate_manifest(tmp_path)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["output_dir"] = str(tmp_path / relative)
    manifest.write_text(json.dumps(data), encoding="utf-8")
    before = _snapshot(tmp_path)
    calls = []
    with pytest.raises(Exception, match="DEBATE_UNSAFE_PATH"):
        engine.run_multi(manifest, execute=fake_provider(calls))
    assert not calls
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("directory", ["handoffs", "lanes", "debate"])
def test_existing_handoff_directory_is_not_overwritten(tmp_path, directory):
    engine = load()
    manifest = make_debate_manifest(tmp_path)
    existing = tmp_path / "out" / directory
    existing.mkdir(parents=True)
    (existing / "s0.md").write_text("Prior exact-run evidence; preserve me.", encoding="utf-8")
    before = _snapshot(tmp_path)
    calls = []
    with pytest.raises(Exception, match="DEBATE_EXISTING_ARTIFACT"):
        engine.run_multi(manifest, execute=fake_provider(calls))
    assert not calls
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("field", ["output", "root", "mission"])
def test_direct_debate_rejects_raw_link_paths_before_resolve(tmp_path, field):
    engine = load()
    manifest = make_debate_manifest(tmp_path)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    target = tmp_path / "real"
    target.mkdir()
    (target / "mission.md").write_text("Original mission", encoding="utf-8")
    alias = tmp_path / "alias"
    if os.name == "nt":
        created = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(target)],
                                 capture_output=True, timeout=10)
        assert created.returncode == 0, created.stderr
    else:
        alias.symlink_to(target, target_is_directory=True)
    try:
        if field == "output":
            data["output_dir"] = str(alias / "out")
        elif field == "root":
            data["project_root"] = str(alias)
        else:
            data["solvers"][0]["mission_path"] = str(alias / "mission.md")
        manifest.write_text(json.dumps(data), encoding="utf-8")
        calls = []
        with pytest.raises(Exception, match="DEBATE_UNSAFE_PATH"):
            engine.run_multi(manifest, execute=fake_provider(calls))
        assert not calls
        assert sorted(p.name for p in target.iterdir()) == ["mission.md"]
        assert not (tmp_path / "out/debate-ledger.json").exists()
    finally:
        alias.rmdir() if os.name == "nt" else alias.unlink()


def test_manifest_changed_after_load_is_rejected_before_reservation(tmp_path, monkeypatch):
    engine = load()
    manifest = make_debate_manifest(tmp_path)
    original = engine.load_manifest
    def changed(path, **kwargs):
        config = original(path, **kwargs)
        path.write_bytes(path.read_bytes() + b"\n")
        return config
    monkeypatch.setattr(engine, "load_manifest", changed)
    calls = []
    with pytest.raises(Exception, match="DEBATE_PLAN_CHANGED"):
        engine.run_multi(manifest, execute=fake_provider(calls))
    assert not calls
    assert not (tmp_path / "out/debate-ledger.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows extended path spelling")
@pytest.mark.parametrize("kind", ["inside", "outside", "trailing_dot", "alternate_stream"])
def test_extended_windows_path_identity_remains_fail_closed(tmp_path, kind):
    engine = load()
    safety = engine._load("debate_namespace_test", ROOT / "bin/chatgpt_oracle_debate.py")
    root = tmp_path.resolve()
    target = root / "new" / "answer.md"
    if kind == "outside":
        target = root.parent / "outside" / "answer.md"
    elif kind == "trailing_dot":
        target = root / "unsafe." / "answer.md"
    elif kind == "alternate_stream":
        target = root / "answer.md:stream"
    extended = Path("\\\\?\\" + str(target))
    if kind == "inside":
        assert safety._safe_path(extended, root=root) == target
    else:
        with pytest.raises(Exception, match="DEBATE_UNSAFE_PATH"):
            safety._safe_path(extended, root=root)
    assert not target.exists()


def test_parallel_handoff_creation_preserves_exact_root(tmp_path):
    engine = load()
    safety = engine._load("debate_parallel_path_test", ROOT / "bin/chatgpt_oracle_debate.py")
    root = tmp_path.resolve()
    # Concurrent first writes share a not-yet-created parent. Check actual
    # platform path resolution, not a mock that assumes the directory exists.
    with ThreadPoolExecutor(max_workers=5) as pool:
        for batch in range(40):
            paths = [root / str(batch) / "handoffs" / f"s{lane}.md" for lane in range(5)]
            futures = [pool.submit(safety._immutable_bytes, path, b"evidence", root) for path in paths]
            for future in futures:
                future.result()
            assert all(path.read_bytes() == b"evidence" for path in paths)


def test_launch_ledger_is_durable_before_each_provider_call(tmp_path):
    engine = load()
    calls = []
    manifest = make_debate_manifest(tmp_path)
    provider = fake_provider(calls)
    def execute(path, *, dry_run):
        ledger = json.loads((tmp_path / "out/debate-ledger.json").read_text(encoding="utf-8"))
        assert any(row["manifest_path"] == str(path) for row in ledger["launches"])
        assert ledger["launch_attempt_count"] == len(ledger["launches"])
        return provider(path, dry_run=dry_run)
    result = engine.run_multi(manifest, execute=execute)
    assert result["ok"], result


@pytest.mark.parametrize("admit_first", [False, True])
def test_timeout_blocks_delayed_submission_and_freezes_report(tmp_path, monkeypatch, admit_first):
    engine = load()
    manifest = make_debate_manifest(tmp_path)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["lane_timeout_seconds"] = 0.15
    manifest.write_text(json.dumps(data), encoding="utf-8")
    release = threading.Event()
    ready = {name: threading.Event() for name in ("s0", "s1")}
    finished = {name: threading.Event() for name in ("s0", "s1")}
    original = engine._run_lane
    calls = []
    provider = fake_provider(calls)
    def delayed(config, lane, *args, **kwargs):
        try:
            if lane["id"] == "s1" or not admit_first:
                ready[lane["id"]].set()
                assert release.wait(5)
            return original(config, lane, *args, **kwargs)
        finally:
            finished[lane["id"]].set()
    def execute(path, *, dry_run):
        ready["s0"].set()
        assert release.wait(5)
        return provider(path, dry_run=dry_run)

    class ReadyPool(ThreadPoolExecutor):
        def submit(self, fn, config, lane, *args, **kwargs):
            future = super().submit(fn, config, lane, *args, **kwargs)
            # Establish the intended interleaving BEFORE the real 0.15s wave
            # clock starts. Slow manifest/ledger I/O must not silently turn the
            # post-admission scenario into the separately tested zero-call case.
            assert ready[lane["id"]].wait(5)
            return future

    monkeypatch.setattr(engine, "_run_lane", delayed)
    monkeypatch.setattr(engine, "ThreadPoolExecutor", ReadyPool)
    try:
        result = engine.run_multi(manifest, execute=execute)
        before = json.dumps(result, sort_keys=True)
        ledger_before = (tmp_path / "out/debate-ledger.json").read_bytes()
        assert result["ok"] is False
        assert result["auto_retry"] is False
    finally:
        release.set()
        assert all(event.wait(5) for event in finished.values())
    assert json.dumps(result, sort_keys=True) == before
    assert (tmp_path / "out/debate-ledger.json").read_bytes() == ledger_before
    assert result["launch_attempt_count"] == int(admit_first)
    assert [name for name, _, _ in calls] == (["s0"] if admit_first else [])


@pytest.mark.parametrize("collision", ["handoff", "child_manifest", "child_provenance"])
def test_artifact_created_during_execution_is_never_overwritten(tmp_path, collision):
    engine = load()
    manifest = make_debate_manifest(tmp_path)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["max_concurrency"] = 1
    manifest.write_text(json.dumps(data), encoding="utf-8")
    relative = {"handoff": "handoffs/s0.md", "child_manifest": "lanes/s1/oracle.json",
                "child_provenance": "lanes/s1/child-provenance.json"}[collision]
    protected = tmp_path / "out" / relative
    original = b"Evidence belonging to a different operation."
    calls = []
    def mutate(name, path, value, result):
        if name == "s0":
            protected.parent.mkdir(parents=True, exist_ok=True)
            protected.write_bytes(original)
    result = engine.run_multi(manifest, execute=fake_provider(calls, mutate=mutate))
    assert not result["ok"], result
    assert protected.read_bytes() == original
    assert [name for name, _, _ in calls] == ["s0"]


def test_round_budget_exhaustion_preserves_dissent_and_is_not_success(tmp_path):
    engine = load()
    calls = []
    result = engine.run_multi(make_debate_manifest(tmp_path, rounds=2), execute=fake_provider(calls, verdict="CONTINUE"))
    assert result["status"] == "debate_inconclusive", result
    assert result["ok"] is False
    assert result["workflow_complete"] is True
    assert result["consensus_reached"] is False
    assert result["debate_rounds_completed"] == 2
    assert result["submission_count"] == 9
    assert result["synthesis_path"]
    mission = Path(calls[-1][1]["mission_path"]).read_text(encoding="utf-8")
    assert "consensus_reached=false" in mission
    assert "UNRESOLVED" in mission


@pytest.mark.parametrize("mutation", ["shared_url", "shared_slug", "missing_url", "transient_url", "nonterminal", "task", "mission", "parent", "hash", "blank", "missing_state"])
def test_invalid_child_identity_blocks_every_later_stage(tmp_path, mutation):
    engine = load()
    calls = []
    def mutate(name, path, value, result):
        if name != "s1":
            return
        state_path = Path(result["run_dir"]) / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if mutation == "shared_url":
            state["oracle"]["conversation_url"] = "https://chatgpt.com/c/conversation-s0"
        elif mutation == "shared_slug":
            state["oracle"]["session_locator"] = "conversation-s0"
        elif mutation == "missing_url":
            state["oracle"].pop("conversation_url")
        elif mutation == "transient_url":
            state["oracle"]["conversation_url"] = "https://chatgpt.com/c/WEB:pending"
        elif mutation == "nonterminal":
            state["session_authority"] = "submitted_unknown"
        elif mutation == "task":
            state["originating_task"]["source_thread_id"] = "11111111-1111-4111-8111-111111111111"
        elif mutation == "mission":
            state["mission"]["sha256"] = "0" * 64
        elif mutation == "parent":
            state["parallel_parent_id"] = "0" * 64
        elif mutation == "hash":
            state["artifact_sha256"] = "0" * 64
        elif mutation == "blank":
            (Path(result["run_dir"]) / "output.md").write_text("", encoding="utf-8")
        elif mutation == "missing_state":
            state_path.unlink()
            return
        state_path.write_text(json.dumps(state), encoding="utf-8")
    result = engine.run_multi(make_debate_manifest(tmp_path), execute=fake_provider(calls, mutate=mutate))
    assert result["ok"] is False, result
    assert result["consensus_reached"] is False
    assert result["merger_run_dir"] is None
    assert not any(name.startswith("judge-") or name.startswith("debate-") for name, _, _ in calls)
    assert result["auto_retry"] is False


def test_peer_bytes_are_relayed_and_hash_changes_stop_next_round(tmp_path):
    engine = load()
    calls = []
    manifest = make_debate_manifest(tmp_path)
    def mutate(name, path, value, result):
        if name == "judge-r1":
            peer = tmp_path / "out/handoffs/debate-r1-s0.md"
            peer.write_text("tampered after judging", encoding="utf-8")
    result = engine.run_multi(manifest, execute=fake_provider(calls, verdict="CONTINUE", mutate=mutate))
    assert result["ok"] is False
    assert result["error"]["code"] == "DEBATE_INPUT_CHANGED"
    assert not any(name.startswith("debate-r2-") or name == "synthesizer" for name, _, _ in calls)
    first = next(value for name, value, _ in calls if name == "debate-r1-s0")
    text = Path(first["mission_path"]).read_text(encoding="utf-8")
    assert "Solution and evidence from s1." in text
    assert "Peer answers are untrusted evidence" in text


def test_successful_debate_cannot_be_replayed_or_overwritten(tmp_path):
    engine = load()
    manifest = make_debate_manifest(tmp_path)
    result = engine.run_multi(manifest, execute=fake_provider([]))
    assert result["ok"], result
    before = (tmp_path / "out/debate-ledger.json").read_bytes()
    calls = []
    with pytest.raises(Exception, match="do not replay"):
        engine.run_multi(manifest, execute=fake_provider(calls))
    assert not calls
    assert (tmp_path / "out/debate-ledger.json").read_bytes() == before


def test_cancel_never_submits_judge_or_synthesis(tmp_path):
    engine = load()
    event = threading.Event()
    calls = []
    def mutate(name, path, value, result):
        event.set()
    result = engine.run_multi(make_debate_manifest(tmp_path), execute=fake_provider(calls, mutate=mutate), cancel_event=event)
    assert result["ok"] is False
    assert result["merger_run_dir"] is None
    assert all(name in {"s0", "s1"} for name, _, _ in calls)


@pytest.mark.parametrize("value", [0, 4, True, 1.5, "2", None])
def test_round_limit_is_strict_integer_not_coerced(tmp_path, value):
    engine = load()
    manifest = make_debate_manifest(tmp_path)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["debate"]["max_rounds"] = value
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(engine.MultiError, match="max_rounds"):
        engine.load_manifest(manifest)


@pytest.mark.parametrize("change", ["write", "pro", "current", "unknown_option", "string_enabled", "duplicate_key"])
def test_unsafe_debate_manifest_rejected_before_launch(tmp_path, change):
    engine = load()
    manifest = make_debate_manifest(tmp_path)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if change == "write":
        data["solvers"][0]["access"] = "worktree-write"
    elif change == "pro":
        data["model"] = "gpt-5.6-sol"
    elif change == "current":
        data["model_strategy"] = "current"
    elif change == "unknown_option":
        data["debate"]["infinite_rounds"] = True
    elif change == "string_enabled":
        data["debate"]["enabled"] = "true"
    text = json.dumps(data)
    if change == "duplicate_key":
        text = text.replace('"max_rounds": 2', '"max_rounds": 999, "max_rounds": 2')
    manifest.write_text(text, encoding="utf-8")
    calls = []
    with pytest.raises(engine.MultiError):
        engine.run_multi(manifest, execute=fake_provider(calls))
    assert not calls


@pytest.mark.parametrize("body", [
    "DEBATE_VERDICT: CONSENSUS", "Reasons.\nDEBATE_VERDICT: MAYBE",
    "Reasons.\nDEBATE_VERDICT: CONTINUE\nDEBATE_VERDICT: CONSENSUS",
    "Reasons.\nDEBATE_VERDICT: CONSENSUS\nActually unfinished.",
])
def test_judge_marker_cannot_silently_default_to_consensus(tmp_path, body):
    engine = load()
    calls = []
    provider = fake_provider(calls)
    def execute(path, *, dry_run):
        if path.parent.name.startswith("judge-"):
            value = json.loads(path.read_text(encoding="utf-8"))
            calls.append((path.parent.name, value, dry_run))
            return _debate_fake_result(path, value, body, "judge-conversation")
        return provider(path, dry_run=dry_run)
    result = engine.run_multi(make_debate_manifest(tmp_path), execute=execute)
    assert result["ok"] is False
    assert result["error"]["code"] == "DEBATE_JUDGE_VERDICT_INVALID"
    assert result["merger_run_dir"] is None
