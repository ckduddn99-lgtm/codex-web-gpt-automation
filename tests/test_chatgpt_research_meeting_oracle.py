"""Oracle adapter contract fixtures. No browser, actual account or web submission."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load():
    name = "research_meeting_native_contract_test"
    spec = importlib.util.spec_from_file_location(name, ROOT / "bin/chatgpt_research_meeting_oracle.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    module = load()
    private, public, storage = (tmp_path / name for name in ("private", "public", "native-fixture"))
    private.mkdir()
    public.mkdir()
    storage.mkdir()
    brief = {"summary": "Public reliability question", "topics": {"docs": "Public official documentation"}}
    path = module.CORE.write_plan(private / "meeting/plan.json", project_root=private,
                                  task="PRIVATE_FIXTURE_DO_NOT_SEARCH", public_brief=brief, public_root=public)
    digest = module.CORE._sha(path.read_bytes())
    plan = json.loads(path.read_text(encoding="utf-8"))
    calls, qualified, controls = [], [], {"unresolved": [], "native_error": False, "mutate": None, "browser_receipt": True}
    thread = "11111111-1111-4111-8111-111111111111"  # synthetic fixture, never injected into environment
    state = SimpleNamespace(
        SCHEMA="synthetic.oracle.fixture/v1", current_source_thread_id=lambda: thread,
        oracle_state_root=lambda: storage,
        unresolved_project_sessions=lambda *args, **kwargs: controls["unresolved"],
        source_thread_id_from_state=lambda value: value["originating_task"]["source_thread_id"],
        proven_ownership_receipt=lambda path: {"synthetic": True},
        proven_browser_identity_receipt=lambda path: {"synthetic": True} if controls["browser_receipt"] else None,
    )
    def execute(manifest_path, *, dry_run):
        assert dry_run is False
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        calls.append(manifest)
        if controls["native_error"]:
            return {"ok": False, "error": {"code": "SYNTHETIC_NATIVE_FAILURE"}}
        mission = Path(manifest["mission_path"])
        request = json.loads(mission.read_text(encoding="utf-8").split("[REQUEST_UNTRUSTED_DATA]\n", 1)[1])
        key = module.hashlib.sha256(manifest["project_root"].casefold().encode()).hexdigest()[:24]
        run_dir = storage / "projects" / key / "runs" / ("fixture-" + str(len(calls)))
        run_dir.mkdir(parents=True)
        body = {"action": "pass", "text": "Synthetic adapter response; not actual research.", "seen_through": request["seen_through"],
                "reply_to": [], "evidence": [], "topic_ids": [], "resolves": []}
        raw = ('BEGIN_MEETING_RESPONSE\n' + json.dumps(body) + '\nEND_MEETING_RESPONSE\nTASK_OUTCOME: EXECUTED\n').encode()
        (run_dir / "output.md").write_bytes(raw)
        slug = "fixture-oracle-" + str(len(calls))
        value = {"run_id": run_dir.name, "status": "complete", "session_authority": "terminal", "terminal_harvested": True,
                 "task_outcome_contract": "v1", "task_outcome": "executed", "project_root": manifest["project_root"],
                 "parallel_parent_id": manifest["parallel_parent_id"], "mission": {"sha256": module.CORE._sha(mission.read_bytes())},
                 "artifact_sha256": module.CORE._sha(raw), "originating_task": {"source_thread_id": thread},
                 "oracle": {"slug": slug, "session_locator": slug, "conversation_url": "https://chatgpt.com/c/" + slug}}
        if controls["mutate"]:
            controls["mutate"](value)
        (run_dir / "state.json").write_bytes(module.CORE._bytes(value))
        return {"ok": True, "run_dir": str(run_dir)}
    dependencies = {
        "research_meeting_oracle_state": state,
        "research_meeting_devspace_preflight": SimpleNamespace(ensure_exact_root_qualified=lambda root: qualified.append(root)),
        "research_meeting_native_runner": SimpleNamespace(execute_run=execute),
        "research_meeting_workspace_config": SimpleNamespace(configured_app_name=lambda: "FixtureApp"),
    }
    monkeypatch.setattr(module, "_load", lambda name, path: dependencies[name])
    def build(): return module.OracleProvider(plan, path, digest)
    def request(web=True, number=1):
        return {"actor": "researcher" if web else "analyst", "phase": "initial", "round": 0, "web_research": web,
                "public_brief": brief, "topic_ids": ["docs"] if web else [],
                "task": brief["summary"] if web else plan["task"], "messages": [], "seen_through": 0,
                "private_notes": "", "open_objections": [],
                "turn_id": f"turn-{number:03d}-" + ("researcher" if web else "analyst")}
    return SimpleNamespace(module=module, private=private, public=public, plan=plan, path=path,
                           calls=calls, qualified=qualified, controls=controls, build=build, request=request)


def test_public_native_turn_has_separate_root_and_no_private_task(fixture):
    f = fixture
    provider = f.build()
    reply = provider(f.request())
    assert f.qualified == [f.private, f.public]
    assert len(f.calls) == 1
    manifest = f.calls[0]
    assert manifest["project_root"] == str(f.public)
    assert manifest["model"] == "gpt-5.6" and manifest["thinking_time"] == "extra-high"
    assert manifest["model_strategy"] == "select" and manifest["research"] == "off"
    assert manifest["task_outcome_contract"] == "v1"
    assert "PRIVATE_FIXTURE" not in Path(manifest["mission_path"]).read_text(encoding="utf-8")
    assert reply["provenance"]["kind"] == "oracle_terminal"  # synthetic native receipts, not live evidence
    assert reply["provenance"]["web_search_verified"] is False
    assert reply["body"]["action"] == "pass"


def test_private_turn_does_not_enter_public_workspace(fixture):
    provider = fixture.build()
    provider(fixture.request(web=False))
    manifest = fixture.calls[0]
    assert manifest["project_root"] == str(fixture.private)
    assert "PRIVATE_FIXTURE" in Path(manifest["mission_path"]).read_text(encoding="utf-8")
    assert list(fixture.public.iterdir()) == []


@pytest.mark.parametrize("mutation", ["private_message", "private_note", "extra_private_field", "unapproved_topic"])
def test_private_context_is_rejected_before_public_native_call(fixture, mutation):
    request = fixture.request()
    if mutation == "private_message": request["messages"] = [{"text": "PRIVATE_FIXTURE"}]
    elif mutation == "private_note": request["private_notes"] = "PRIVATE_FIXTURE"
    elif mutation == "extra_private_field": request["private_payload"] = "PRIVATE_FIXTURE"
    else: request["topic_ids"] = ["PRIVATE_FIXTURE"]
    with pytest.raises(Exception, match="PUBLIC_REQUEST_INVALID|REQUEST_INVALID"):
        fixture.build()(request)
    assert not fixture.calls
    assert list(fixture.public.iterdir()) == []


def test_native_failure_is_durable_and_cannot_be_replayed(fixture):
    provider = fixture.build()
    fixture.controls["native_error"] = True
    request = fixture.request()
    with pytest.raises(Exception, match="NATIVE_TURN_UNCERTAIN"):
        provider(request)
    with pytest.raises(Exception, match="TURN_REPLAY"):
        provider(request)
    assert len(fixture.calls) == 1
    assert (provider.private_output / request["turn_id"] / "native-result.json").is_file()


@pytest.mark.parametrize("mutation", ["task", "root", "mission", "parent", "nonterminal", "output_hash", "browser_receipt"])
def test_native_identity_contradictions_never_become_verified_results(fixture, mutation):
    provider = fixture.build()
    def corrupt(state):
        if mutation == "task": state["originating_task"]["source_thread_id"] = "22222222-2222-4222-8222-222222222222"
        elif mutation == "root": state["project_root"] = str(fixture.private)
        elif mutation == "mission": state["mission"]["sha256"] = "0" * 64
        elif mutation == "parent": state["parallel_parent_id"] = "0" * 64
        elif mutation == "nonterminal": state["terminal_harvested"] = False
        elif mutation == "output_hash": state["artifact_sha256"] = "0" * 64
    fixture.controls["mutate"] = corrupt
    if mutation == "browser_receipt": fixture.controls["browser_receipt"] = False
    with pytest.raises(Exception, match="NATIVE_BINDING_INVALID"):
        provider(fixture.request())
    assert len(fixture.calls) == 1


def test_unresolved_native_admission_blocks_before_turn_creation(fixture):
    fixture.controls["unresolved"] = [{"run_id": "synthetic-unresolved"}]
    with pytest.raises(Exception, match="UNRESOLVED_NATIVE_RUN"):
        fixture.build()
    assert not fixture.calls
    assert list(fixture.public.iterdir()) == []


def test_public_scope_drift_blocks_before_native_submission(fixture):
    provider = fixture.build()
    (fixture.public / "unapproved.txt").write_text("Do not send this", encoding="utf-8")
    with pytest.raises(Exception, match="PUBLIC_SCOPE_CHANGED"):
        provider(fixture.request())
    assert not fixture.calls


def test_changed_reviewed_plan_blocks_before_native_submission(fixture):
    provider = fixture.build()
    fixture.path.write_bytes(fixture.path.read_bytes() + b"\n")
    with pytest.raises(Exception, match="PLAN_CHANGED"):
        provider(fixture.request())
    assert not fixture.calls
