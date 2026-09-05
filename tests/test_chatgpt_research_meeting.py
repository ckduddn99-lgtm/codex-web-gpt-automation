"""Deterministic meeting simulations; never evidence of live web execution."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "bin/chatgpt_research_meeting.py"


def load():
    spec = importlib.util.spec_from_file_location("research_meeting_test", PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def brief():
    return {"summary": "Public browser automation recovery research.", "topics": {
        "durability": "Find official documentation about durable browser session metadata.",
        "counterexamples": "Find public counterexamples involving browser disconnects.",
    }}


def plan(module, tmp_path, **budgets):
    return module.write_plan(tmp_path / "meeting" / "plan.json", project_root=tmp_path,
                             task="PRIVATE_PROJECT_SENTINEL: review internal recovery design.",
                             public_brief=brief(), **budgets)


def card():
    return {"url": "https://example.org/documentation#session", "title": "Synthetic source",
            "claim": "A simulated counterexample exists.", "excerpt": "Synthetic fixture only.",
            "published_at": "2026-01-01", "accessed_at": "2026-09-05",
            "scope": "Synthetic test version; applicability not established."}


class Provider:
    simulation = True

    def __init__(self, mutate=None):
        self.requests = []
        self.mutate = mutate

    def __call__(self, request):
        self.requests.append(copy.deepcopy(request))
        action = {"initial": "claim", "research": "claim", "react": "pass",
                  "close": "agree", "synthesize": "synthesize"}[request["phase"]]
        body = {"action": action, "text": "Synthetic analysis, not a live agent result.",
                "seen_through": request["seen_through"], "reply_to": [],
                "evidence": [card()] if request["web_research"] else [],
                "topic_ids": [], "resolves": []}
        if self.mutate:
            self.mutate(request, body)
        return {"body": body, "provenance": {"kind": "synthetic", "turn_id": request["turn_id"]}}


def run(module, path, provider=None, **kwargs):
    return module.run_meeting(path, expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                              provider=provider or Provider(), **kwargs)


def test_spontaneous_objection_is_not_host_authored_or_directly_requested(tmp_path):
    module = load()
    def dissent(request, body):
        if request["phase"] == "react" and request["actor"] == "skeptic":
            claim = next(m for m in request["messages"] if m["actor"] == "analyst")
            body.update(action="object", text="I independently object to A's assumption.", reply_to=[claim["id"]])
    provider = Provider(dissent)
    result = run(module, plan(module, tmp_path, review_rounds=1), provider)
    events = module.read_events(Path(result["events_dir"]))
    objection = next(e for e in events if e["kind"] == "message" and e["data"]["action"] == "object")
    assert objection["data"]["actor"] == "skeptic"
    assert objection["data"]["text"] == "I independently object to A's assumption."
    request = next(r for r in provider.requests if r["phase"] == "react" and r["actor"] == "skeptic")
    assert "requested_action" not in request
    assert result["consensus_reached"] is False
    assert result["solution_verified"] is False
    assert result["status"] == "inconclusive"
    assert result["open_objections"]


def test_web_jobs_receive_only_public_brief_not_private_task_or_meeting(tmp_path):
    module = load()
    provider = Provider()
    result = run(module, plan(module, tmp_path), provider)
    web = [r for r in provider.requests if r["web_research"]]
    assert web
    assert all("PRIVATE_PROJECT_SENTINEL" not in json.dumps(r) for r in web)
    assert all(r["messages"] == [] and r["private_notes"] == "" for r in web)
    assert all(r["public_brief"] == brief() for r in web)
    assert result["web_search_verified"] is False
    assert result["simulation"] is True
    assert result["verified_web_session_count"] == 0


def test_research_requests_are_deduplicated_and_return_to_meeting(tmp_path):
    module = load()
    def request_research(request, body):
        if request["phase"] == "react" and request["round"] == 1 and request["actor"] in {"analyst", "skeptic"}:
            body.update(action="research", topic_ids=["counterexamples"], text="Need the approved public counterexample topic.")
    provider = Provider(request_research)
    result = run(module, plan(module, tmp_path, review_rounds=2), provider)
    jobs = [r for r in provider.requests if r["phase"] == "research"]
    assert len(jobs) == 1
    assert jobs[0]["topic_ids"] == ["counterexamples"]
    assert result["research_jobs"] == 1
    assert any(any(m.get("phase") == "research" for m in r["messages"])
               for r in provider.requests if r["phase"] == "react" and r["round"] == 2)


def test_pass_and_unreviewed_are_never_consensus(tmp_path):
    module = load()
    def abstain(request, body):
        if request["phase"] == "close" and request["actor"] == "skeptic":
            body["action"] = "pass"
    result = run(module, plan(module, tmp_path), Provider(abstain))
    assert result["status"] == "inconclusive"
    assert result["reviews"]["skeptic"] == "pass"
    assert result["consensus_reached"] is False


def test_only_objection_author_can_resolve_it(tmp_path):
    module = load()
    def mutate(request, body):
        if request["phase"] == "react" and request["round"] == 1 and request["actor"] == "skeptic":
            body.update(action="object", reply_to=[request["messages"][0]["id"]])
        if request["phase"] == "react" and request["round"] == 2 and request["actor"] == "analyst":
            body.update(action="revise", resolves=[request["open_objections"][0]["id"]])
    result = run(module, plan(module, tmp_path, review_rounds=2), Provider(mutate))
    assert result["status"] == "attention_required"
    assert result["error"]["code"] == "OBJECTION_OWNER_MISMATCH"


def test_final_review_rejects_new_objection_instead_of_accepting_earlier_agreements(tmp_path):
    module = load()
    def late(request, body):
        if request["phase"] == "close" and request["actor"] == "skeptic":
            body.update(action="object", reply_to=[request["messages"][0]["id"]], text="Late blocking counterexample.")
    result = run(module, plan(module, tmp_path), Provider(late))
    assert not result["ok"]
    assert result["open_objections"]


@pytest.mark.parametrize("mutation", ["future_cursor", "unknown_reference", "unknown_topic", "fake_verified", "extra_key"])
def test_invalid_turn_fails_closed(tmp_path, mutation):
    module = load()
    def invalid(request, body):
        if request["actor"] != "analyst" or request["phase"] != "initial":
            return
        if mutation == "future_cursor": body["seen_through"] += 1
        elif mutation == "unknown_reference": body["reply_to"] = [99999]
        elif mutation == "unknown_topic": body.update(action="research", topic_ids=["leak-private-data"])
        elif mutation == "fake_verified": body["evidence"] = [{**card(), "verified": True}]
        else: body["host_execute"] = "arbitrary command"
    result = run(module, plan(module, tmp_path), Provider(invalid))
    assert result["status"] == "attention_required"
    assert not result["ok"]


def test_plan_hash_drift_blocks_before_provider_or_output(tmp_path):
    module = load()
    path = plan(module, tmp_path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_bytes(path.read_bytes() + b"\n")
    provider = Provider()
    with pytest.raises(module.MeetingError, match="PLAN_CHANGED"):
        module.run_meeting(path, expected_sha256=digest, provider=provider)
    assert provider.requests == []
    assert not (path.parent / "run").exists()


def test_replay_never_overwrites_events(tmp_path):
    module = load()
    path = plan(module, tmp_path)
    first = run(module, path)
    before = {p.name: p.read_bytes() for p in Path(first["events_dir"]).iterdir()}
    provider = Provider()
    with pytest.raises(module.MeetingError, match="EXISTING_RUN"):
        run(module, path, provider)
    assert not provider.requests
    assert {p.name: p.read_bytes() for p in Path(first["events_dir"]).iterdir()} == before


def test_event_tampering_stops_next_admission(tmp_path):
    module = load()
    path = plan(module, tmp_path, max_concurrency=1)
    def tamper(request, body):
        if request["actor"] == "analyst" and request["phase"] == "initial":
            event = sorted((path.parent / "run/events").glob("*.json"))[0]
            event.write_bytes(event.read_bytes().replace(b"started", b"changed"))
    result = run(module, path, Provider(tamper))
    assert result["status"] == "attention_required"
    assert result["error"]["code"] == "EVENT_LOG_CHANGED"


def test_cancel_before_start_has_zero_calls(tmp_path):
    module = load()
    cancelled = threading.Event()
    cancelled.set()
    provider = Provider()
    result = run(module, plan(module, tmp_path), provider, cancel_event=cancelled)
    assert not provider.requests
    assert result["status"] == "cancelled"
    assert result["launch_attempt_count"] == 0


def test_budget_reserves_every_final_review_and_synthesis(tmp_path):
    module = load()
    provider = Provider()
    result = run(module, plan(module, tmp_path, max_calls=9), provider)
    assert len(provider.requests) == 9  # four initial + four explicit final reviews + synthesis
    assert result["launch_attempt_count"] == 9
    assert len([r for r in provider.requests if r["phase"] == "close"]) == 4
    assert provider.requests[-1]["phase"] == "synthesize"


def test_public_url_normalization_and_rejected_credentials():
    module = load()
    assert module.canonical_url("https://EXAMPLE.org/a#fragment") == "https://example.org/a"
    for value in ["http://example.org/a", "https://user:pass@example.org/", "https://127.0.0.1/a",
                  "file:///secret", "https://localhost/a", "https://example.org:8443/a"]:
        with pytest.raises(module.MeetingError): module.canonical_url(value)


def test_terminal_view_escapes_control_sequences(tmp_path):
    module = load()
    def unsafe(request, body): body["text"] = "\x1b[31mnot-a-terminal-command\x00"
    result = run(module, plan(module, tmp_path), Provider(unsafe))
    text = module.render_events(Path(result["events_dir"]))
    assert "\x1b" not in text and "\x00" not in text
    assert "not-a-terminal-command" in text


def test_live_cli_never_accepts_fabricated_task_identity(tmp_path, monkeypatch):
    module = load()
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    path = plan(module, tmp_path)
    with pytest.raises(module.MeetingError, match="TASK_ID_REQUIRED"):
        module.run_meeting(path, expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    assert not (path.parent / "run").exists()


def test_event_validation_checks_shared_parent_once_without_skipping_bytes(tmp_path, monkeypatch):
    module = load()
    store = module.EventStore(tmp_path / "events", tmp_path)
    for number in range(3):
        store.append("test", {"number": number})
    original = module.SAFETY._safe_path
    calls = []
    def counted(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(module.SAFETY, "_safe_path", counted)
    assert len(module.read_events(store.directory)) == 3
    assert len(calls) <= 2, "shared ancestors must not be walked again for every event"
    target = store.directory / "000002.json"
    target.write_bytes(target.read_bytes().replace(b'"number":1', b'"number":9'))
    with pytest.raises(module.MeetingError, match="EVENT_LOG_CHANGED"):
        module.read_events(store.directory)


def test_failed_research_stays_pending_and_is_not_silently_consensus(tmp_path):
    module = load()
    def no_sources(request, body):
        if request["phase"] == "react" and request["actor"] == "analyst":
            body.update(action="research", topic_ids=["counterexamples"])
        if request["phase"] == "research":
            body.update(action="pass", evidence=[], text="Web tools unavailable; not researched.")
    result = run(module, plan(module, tmp_path, review_rounds=1), Provider(no_sources))
    assert result["pending_research"] == ["counterexamples"]
    assert result["consensus_reached"] is False


def test_last_minute_withdrawal_requires_a_new_decision_snapshot(tmp_path):
    module = load()
    def retract(request, body):
        if request["phase"] == "react" and request["actor"] == "skeptic":
            body.update(action="object", reply_to=[request["messages"][0]["id"]])
        if request["phase"] == "close" and request["actor"] == "skeptic":
            body.update(action="agree", resolves=[request["open_objections"][0]["id"]])
    result = run(module, plan(module, tmp_path, review_rounds=1), Provider(retract))
    assert not result["open_objections"]
    assert result["consensus_reached"] is False


def load_adapter():
    spec = importlib.util.spec_from_file_location("meeting_adapter_test", ROOT / "bin/chatgpt_research_meeting_oracle.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("text", [
    'BEGIN_MEETING_RESPONSE\n{"action":"pass","action":"agree"}\nEND_MEETING_RESPONSE\nTASK_OUTCOME: EXECUTED',
    'BEGIN_MEETING_RESPONSE\n{}\nEND_MEETING_RESPONSE\nTASK_OUTCOME: NOT_EXECUTED',
    '{}\nTASK_OUTCOME: EXECUTED',
    'BEGIN_MEETING_RESPONSE\n{}\nEND_MEETING_RESPONSE\nTASK_OUTCOME: EXECUTED\nextra instructions',
])
def test_oracle_response_parser_rejects_ambiguous_or_unexecuted_output(text):
    with pytest.raises(Exception):
        load_adapter().parse_output(text.encode())


def test_oracle_response_parser_accepts_exact_structured_result():
    module = load_adapter()
    body = {"action": "pass", "text": "No new objection", "seen_through": 3,
            "reply_to": [], "resolves": [], "topic_ids": [], "evidence": []}
    raw = ('BEGIN_MEETING_RESPONSE\n' + json.dumps(body) + '\nEND_MEETING_RESPONSE\nTASK_OUTCOME: EXECUTED').encode()
    assert module.parse_output(raw) == body


def test_injected_provider_cannot_claim_real_oracle_verification(tmp_path):
    module = load()
    class ForgedProvider(Provider):
        def __call__(self, request):
            response = super().__call__(request)
            response["provenance"] = {"kind": "oracle_terminal", "verified": True, "web_search_verified": True}
            return response
    result = run(module, plan(module, tmp_path, max_calls=9), ForgedProvider())
    assert result["simulation"] is True
    assert result["verified_web_session_count"] == 0
    assert result["web_search_verified"] is False
    assert result["sessions"] == []


def test_view_refreshes_when_final_anchor_arrives_after_snapshot(tmp_path, monkeypatch):
    module = load()
    store = module.EventStore(tmp_path / "events", tmp_path)
    store.append("test", {"message": 1})
    prefix = module.read_events(store.directory)
    store.append("test", {"message": 2})
    (tmp_path / "result.json").write_text(json.dumps({"event_count": 2, "event_tail_sha256": store.hashes[-1]}))
    original = module.read_events
    calls = []
    def snapshots(directory):
        calls.append(directory)
        return prefix if len(calls) == 1 else original(directory)
    monkeypatch.setattr(module, "read_events", snapshots)
    _, count = module._render_snapshot(store.directory)
    assert count == 2
    assert len(calls) == 2


def test_regular_artifact_reader_rejects_reparse_leaf_before_open(tmp_path, monkeypatch):
    module = load()
    path = tmp_path / "artifact.json"
    path.write_bytes(b"{}")
    original = module.Path.lstat
    from types import SimpleNamespace
    def reparse(candidate):
        if candidate == path:
            return SimpleNamespace(st_mode=module.stat.S_IFREG, st_file_attributes=0x400)
        return original(candidate)
    monkeypatch.setattr(module.Path, "lstat", reparse)
    with pytest.raises(module.MeetingError, match="UNSAFE_PATH"):
        module._regular_bytes(path)


def test_runtime_and_tests_are_shipped():
    manifest = json.loads((ROOT / "install-manifest.json").read_text(encoding="utf-8"))
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    for name in ("bin/chatgpt_research_meeting.py", "bin/chatgpt_research_meeting_oracle.py"):
        assert name in manifest["include"]
        assert name in package["files"]
    gate = (ROOT / "scripts/run_fast_gate.py").read_text(encoding="utf-8")
    assert '"tests/test_chatgpt_research_meeting.py"' in gate
    assert '"tests/test_chatgpt_research_meeting_oracle.py"' in gate
    assert "docs/RESEARCH_MEETING.md" in manifest["include"]
    assert "bin/chatgpt_oracle_debate.py" in package["files"]
