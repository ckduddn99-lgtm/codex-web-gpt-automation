#!/usr/bin/env python
"""Bounded research meetings with agent-authored bids and an immutable event log.

No LLM dependency is required by the controller. Injected providers are always
SIMULATIONS. The default provider is the exact-session Oracle adapter; it never
upgrades models, changes root registration, settles runs, or resubmits a turn.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import stat
import sys
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, nullcontext
from pathlib import Path
from typing import Any, BinaryIO, Callable
from urllib.parse import parse_qsl, urlsplit, urlunsplit

BIN = Path(__file__).resolve().parent
SCHEMA = "codex.chatgpt.research-meeting-plan/v1"
RESULT_SCHEMA = "codex.chatgpt.research-meeting-result/v1"
ACTORS = ("analyst", "researcher", "scout", "skeptic")
WEB_ACTORS = {"researcher", "scout"}
BODY_KEYS = {"action", "text", "seen_through", "reply_to", "evidence", "topic_ids", "resolves"}
ACTIONS = {"claim", "object", "revise", "research", "pass", "agree", "synthesize"}
CARD_KEYS = {"url", "title", "claim", "excerpt", "published_at", "accessed_at", "scope"}
MAX_BYTES = 128 * 1024


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SAFETY = _load("research_meeting_path_safety", BIN / "chatgpt_oracle_debate.py")


class MeetingError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def _need(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise MeetingError(code, message)


def _bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                       allow_nan=False) + "\n").encode("utf-8")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json(raw: bytes) -> dict[str, Any]:
    def pairs(items):
        value = {}
        for key, item in items:
            _need(key not in value, "DUPLICATE_KEY", key)
            value[key] = item
        return value
    _need(len(raw) <= MAX_BYTES, "INPUT_TOO_LARGE", "JSON exceeds the bounded input size")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                       parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    _need(isinstance(value, dict), "OBJECT_REQUIRED", "expected one JSON object")
    return value


def _text(value: Any, *, maximum: int = 6000, empty: bool = False) -> bool:
    return isinstance(value, str) and len(value) <= maximum and (empty or bool(value.strip()))


def canonical_url(value: str) -> str:
    """Validate reported source identifiers. This function NEVER fetches a URL."""
    _need(_text(value, maximum=2048) and not any(c.isspace() or ord(c) < 32 for c in value),
          "SOURCE_URL_INVALID", "source must be one public HTTPS URL")
    try:
        parsed = urlsplit(value)
        host = str(parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port
        _need(parsed.scheme == "https" and parsed.username is None and parsed.password is None
              and port in {None, 443} and "." in host and "\\" not in value,
              "SOURCE_URL_INVALID", "credentials, non-HTTPS and nonstandard ports are forbidden")
        _need(not host.endswith((".localhost", ".local", ".internal", ".test", ".invalid"))
              and host != "localhost", "SOURCE_URL_INVALID", "private/local source")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            _need(all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
                      for label in host.split(".")), "SOURCE_URL_INVALID", "invalid public hostname")
        else:
            _need(address.is_global, "SOURCE_URL_INVALID", "non-public address")
        secret_keys = {"key", "api_key", "apikey", "token", "access_token", "auth", "signature", "sig"}
        _need(not any(key.casefold() in secret_keys for key, _ in parse_qsl(parsed.query)),
              "SOURCE_URL_INVALID", "credential-like query parameter")
        return urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))
    except (ValueError, UnicodeError) as exc:
        raise MeetingError("SOURCE_URL_INVALID", "invalid URL") from exc


def _validate_plan(value: dict[str, Any]) -> None:
    expected = {"schema", "project_root", "public_root", "task", "public_brief", "review_rounds",
                "max_calls", "max_research_jobs", "max_concurrency"}
    _need(set(value) == expected and value["schema"] == SCHEMA, "PLAN_INVALID", "unknown plan shape")
    _need(_text(value["task"], maximum=16000), "PLAN_INVALID", "nonempty bounded task required")
    brief = value["public_brief"]
    _need(isinstance(brief, dict) and set(brief) == {"summary", "topics"}
          and _text(brief["summary"], maximum=4000), "PUBLIC_BRIEF_INVALID", "explicit public summary required")
    topics = brief["topics"]
    _need(isinstance(topics, dict) and 1 <= len(topics) <= 12, "PUBLIC_BRIEF_INVALID", "1..12 public topics required")
    for key, text in topics.items():
        _need(re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", key) is not None and _text(text, maximum=2000),
              "PUBLIC_BRIEF_INVALID", "invalid approved topic")
    for name, low, high in (("review_rounds", 1, 3), ("max_calls", 9, 40),
                            ("max_research_jobs", 0, 6), ("max_concurrency", 1, 5)):
        _need(type(value[name]) is int and low <= value[name] <= high, "BUDGET_INVALID", name)
    _need(_text(value["project_root"]) and Path(value["project_root"]).is_absolute(),
          "PROJECT_ROOT_INVALID", "exact absolute project root required")
    root = SAFETY._safe_path(Path(value["project_root"]))
    _need(root.is_dir(), "PROJECT_ROOT_INVALID", "exact project root must already exist")
    if value["public_root"] is not None:
        _need(_text(value["public_root"]) and Path(value["public_root"]).is_absolute(),
              "PUBLIC_ROOT_INVALID", "exact absolute public root required")
        public = SAFETY._safe_path(Path(value["public_root"]))
        _need(public.is_dir() and not public.is_relative_to(root) and not root.is_relative_to(public),
              "PUBLIC_ROOT_INVALID", "public workspace must be an existing separate, non-nested root")


def write_plan(path: Path, *, project_root: Path, task: str, public_brief: dict[str, Any],
               public_root: Path | None = None, review_rounds: int = 2, max_calls: int = 24,
               max_research_jobs: int = 3, max_concurrency: int = 2) -> Path:
    root = SAFETY._safe_path(Path(project_root))
    path = SAFETY._safe_output(Path(path), root=root)
    value = {"schema": SCHEMA, "project_root": str(root), "task": task,
             "public_root": str(SAFETY._safe_path(public_root)) if public_root is not None else None,
             "public_brief": public_brief, "review_rounds": review_rounds, "max_calls": max_calls,
             "max_research_jobs": max_research_jobs, "max_concurrency": max_concurrency}
    _validate_plan(value)
    _need(len(_bytes(value)) <= MAX_BYTES, "PLAN_INVALID", "plan too large")
    SAFETY._immutable_bytes(path, _bytes(value), root)
    return path


def load_plan(path: Path, expected_sha256: str) -> tuple[dict[str, Any], Path]:
    path = SAFETY._safe_path(path)
    raw = path.read_bytes()
    _need(re.fullmatch(r"[a-f0-9]{64}", expected_sha256) is not None and _sha(raw) == expected_sha256,
          "PLAN_CHANGED", "reviewed plan hash must match the exact bytes")
    value = _json(raw)
    _validate_plan(value)
    SAFETY._safe_output(path, root=Path(value["project_root"]))
    return value, path


def _plain_directory(path: Path) -> None:
    info = path.lstat()
    _need(stat.S_ISDIR(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
          "UNSAFE_PATH", "directory changed to a link or non-directory")


class _ArtifactReader:
    """Reuse run-local handles, never artifact bytes or validation results."""

    def __init__(self, stack: ExitStack):
        self.stack = stack
        self.handles: dict[Path, BinaryIO] = {}

    def open(self, path: Path) -> BinaryIO:
        if path not in self.handles:
            # At most 40 turns: request + response + two events per turn,
            # plus the start/final events. No unbounded descriptor cache.
            _need(len(self.handles) < 4 * 40 + 2, "ARTIFACT_LIMIT", "too many retained artifact handles")
            # Buffered readers can serve stale bytes after an in-place edit.
            self.handles[path] = self.stack.enter_context(path.open("rb", buffering=0))
        return self.handles[path]


def _regular_bytes(path: Path, *, limit: int = MAX_BYTES,
                   _reader: _ArtifactReader | None = None) -> bytes:
    # The caller validates the shared ancestors ONCE. Check each leaf's actual
    # handle and identity; still read/hash every byte, never trust only mtimes.
    before = path.lstat()
    _need(stat.S_ISREG(before.st_mode) and not getattr(before, "st_file_attributes", 0) & 0x400,
          "UNSAFE_PATH", "regular non-link artifact required")
    context = path.open("rb") if _reader is None else nullcontext(_reader.open(path))
    with context as stream:
        opened = os.fstat(stream.fileno())
        _need((before.st_dev, before.st_ino) == (opened.st_dev, opened.st_ino),
              "UNSAFE_PATH", "artifact changed during open")
        if _reader is not None:
            stream.seek(0)
        raw = stream.read(limit + 1)
    after = path.lstat()
    _need(stat.S_ISREG(after.st_mode) and not getattr(after, "st_file_attributes", 0) & 0x400
          and (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino),
          "UNSAFE_PATH", "artifact changed during read")
    _need(len(raw) <= limit, "INPUT_TOO_LARGE", "bounded artifact read exceeded")
    return raw


def _publish(path: Path, raw: bytes, root: Path) -> None:
    # Publish an already flushed inode without replacing any destination. The
    # temporary inode is outside events/, so viewers never see a partial record.
    path = SAFETY._safe_path(path, root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent.parent / (".meeting-publish-" + uuid.uuid4().hex)
    SAFETY._immutable_bytes(temporary, raw, root)
    try:
        os.link(temporary, path, follow_symlinks=False)
        if os.name != "nt":
            fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        temporary.unlink()


def read_events(directory: Path, *, _reader: _ArtifactReader | None = None) -> list[dict[str, Any]]:
    directory = SAFETY._safe_path(directory)
    events, previous = [], "0" * 64
    if not directory.is_dir():
        return events
    for index, path in enumerate(sorted(directory.iterdir()), 1):
        _need(path.name == f"{index:06d}.json", "EVENT_LOG_CHANGED", "unexpected or missing event")
        try:
            event = _json(_regular_bytes(path, _reader=_reader))
            digest = event.get("sha256")
            payload = {key: value for key, value in event.items() if key != "sha256"}
            _need(set(payload) == {"sequence", "previous_sha256", "at", "kind", "data"}
                  and event["sequence"] == index and event["previous_sha256"] == previous
                  and digest == _sha(_bytes(payload)), "EVENT_LOG_CHANGED", "broken event chain")
        except (OSError, ValueError, KeyError) as exc:
            raise MeetingError("EVENT_LOG_CHANGED", "event is unreadable or malformed") from exc
        previous = digest
        events.append(event)
    return events


class EventStore:
    def __init__(self, directory: Path, root: Path, *, _reader: _ArtifactReader | None = None):
        self.directory, self.root = directory, root
        self.reader = _reader
        self.hashes: list[str] = []
        directory.mkdir()

    def verify(self) -> None:
        events = read_events(self.directory, _reader=self.reader)
        _need([event["sha256"] for event in events] == self.hashes, "EVENT_LOG_CHANGED", "event prefix changed")

    def append(self, kind: str, data: dict[str, Any]) -> None:
        self.verify()
        event = {"sequence": len(self.hashes) + 1, "previous_sha256": self.hashes[-1] if self.hashes else "0" * 64,
                 "at": dt.datetime.now(dt.timezone.utc).isoformat(), "kind": kind, "data": data}
        event["sha256"] = _sha(_bytes(event))
        raw = _bytes(event)
        _need(len(raw) <= MAX_BYTES, "EVENT_TOO_LARGE", kind)
        _publish(self.directory / f"{event['sequence']:06d}.json", raw, self.root)
        self.hashes.append(event["sha256"])


def _validate_body(body: Any, request: dict[str, Any], objections: dict[int, dict[str, Any]]) -> dict[str, Any]:
    _need(isinstance(body, dict) and set(body) == BODY_KEYS, "TURN_INVALID", "unknown/missing response keys")
    _need(body["action"] in ACTIONS and _text(body["text"]), "TURN_INVALID", "invalid action/text")
    _need(type(body["seen_through"]) is int and body["seen_through"] == request["seen_through"],
          "CURSOR_INVALID", "response must bind the exact delivered snapshot")
    visible = {row["id"] for row in request["messages"]} | {row["id"] for row in request["open_objections"]}
    for name in ("reply_to", "resolves"):
        values = body[name]
        _need(isinstance(values, list) and len(values) <= 16 and all(type(v) is int for v in values)
              and len(set(values)) == len(values) and set(values) <= visible,
              "REFERENCE_INVALID", name)
    if body["action"] == "object":
        _need(bool(body["reply_to"]), "REFERENCE_INVALID", "objections must identify a delivered claim")
    for identifier in body["resolves"]:
        _need(identifier in objections and objections[identifier]["actor"] == request["actor"],
              "OBJECTION_OWNER_MISMATCH", "only its author may withdraw an objection")
        _need(body["action"] in {"revise", "agree"}, "TURN_INVALID", "withdrawal needs explicit revision/agreement")
    topics = body["topic_ids"]
    _need(isinstance(topics, list) and len(topics) <= 3 and all(isinstance(t, str) for t in topics)
          and len(set(topics)) == len(topics) and set(topics) <= set(request["public_brief"]["topics"]),
          "TOPIC_INVALID", "research may reference only user-approved public topic IDs")
    _need(bool(topics) == (body["action"] == "research"), "TOPIC_INVALID", "research requires approved topic IDs")
    if request["web_research"]:
        _need(body["action"] in {"claim", "pass"} and not body["reply_to"] and not body["resolves"],
              "TURN_INVALID", "external research cannot alter private meeting state")
    if request["phase"] == "synthesize":
        _need(body["action"] == "synthesize" and not body["resolves"] and not topics,
              "TURN_INVALID", "synthesis cannot manufacture consensus or new authority")
    else:
        _need(body["action"] != "synthesize", "TURN_INVALID", "wrong phase for synthesis")
    cards = body["evidence"]
    _need(isinstance(cards, list) and len(cards) <= 8, "EVIDENCE_INVALID", "bounded evidence array required")
    normalized = []
    for card in cards:
        _need(isinstance(card, dict) and set(card) == CARD_KEYS, "EVIDENCE_INVALID", "evidence is source-reported, not verified")
        _need(all(_text(card[key], maximum=2000) for key in ("title", "claim", "scope"))
              and _text(card["excerpt"], maximum=500, empty=True) and len(card["excerpt"].split()) <= 25,
              "EVIDENCE_INVALID", "invalid source fields or excessive quotation")
        try:
            dt.date.fromisoformat(card["accessed_at"])
            if card["published_at"] is not None:
                dt.date.fromisoformat(card["published_at"])
        except (ValueError, TypeError) as exc:
            raise MeetingError("EVIDENCE_INVALID", "dates must be ISO dates or unknown publication date") from exc
        normalized.append({**card, "url": canonical_url(card["url"]), "verification": "agent_reported_unverified"})
    _need(not request["web_research"] or body["action"] == "pass" or bool(cards),
          "EVIDENCE_REQUIRED", "web research must cite sources or explicitly abstain")
    return {**copy.deepcopy(body), "evidence": normalized}


def run_meeting(plan_path: Path, *, expected_sha256: str, provider: Callable | None = None,
                cancel_event: threading.Event | None = None) -> dict[str, Any]:
    # Close handles on every exit, including failed setup and result publication.
    # The reader belongs to this controller only; providers/viewers never share it.
    with ExitStack() as stack:
        return _run_meeting(plan_path, expected_sha256=expected_sha256, provider=provider,
                            cancel_event=cancel_event, _reader=_ArtifactReader(stack))


def _run_meeting(plan_path: Path, *, expected_sha256: str, provider: Callable | None,
                 cancel_event: threading.Event | None, _reader: _ArtifactReader) -> dict[str, Any]:
    plan, plan_path = load_plan(plan_path, expected_sha256)
    root = Path(plan["project_root"])
    output = SAFETY._safe_output(plan_path.parent / "run", root=root)
    _need(not output.exists(), "EXISTING_RUN", "one-use meeting; do not replay or overwrite exact turns")
    simulation = provider is not None
    if provider is None:
        adapter = _load("research_meeting_oracle_adapter", BIN / "chatgpt_research_meeting_oracle.py")
        try:
            provider = adapter.OracleProvider(plan, plan_path, expected_sha256)
        except Exception as exc:
            raise MeetingError(getattr(exc, "code", "ORACLE_PREFLIGHT_FAILED"), str(exc)) from exc
    try:
        output.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise MeetingError("EXISTING_RUN", "concurrent meeting already reserved this plan") from exc
    store = EventStore(output / "events", root, _reader=_reader)
    store.append("started", {"schema": SCHEMA, "plan_sha256": expected_sha256, "simulation": simulation})
    history: list[dict[str, Any]] = []
    objections: dict[int, dict[str, Any]] = {}
    cursors = {actor: 0 for actor in ACTORS}
    notes = {actor: "" for actor in ACTORS}
    sources: dict[str, dict[str, Any]] = {}
    researched: set[str] = set()
    research_attempted: set[str] = set()
    pending_initial_research = set(WEB_ACTORS)
    requested: set[str] = set()
    stop_launches = threading.Event()
    result: dict[str, Any] = {
        "schema": RESULT_SCHEMA, "ok": False, "status": "preparing", "simulation": simulation,
        "solution_verified": False, "web_search_verified": False, "verified_web_session_count": 0,
        "conversation_reuse": False, "consensus_reached": False, "workflow_complete": False,
        "launch_attempt_count": 0, "research_jobs": 0, "completed_research_jobs": 0, "review_rounds_completed": 0,
        "plan_sha256": expected_sha256, "events_dir": str(store.directory), "result_path": str(output / "result.json"),
        "reviews": {}, "sessions": [], "auto_retry": False,
    }
    immutable: dict[Path, str] = {plan_path: expected_sha256}

    def check() -> None:
        store.verify()
        SAFETY._safe_path(output, root=root)
        if (output / "turns").exists():
            _plain_directory(output / "turns")
        for path, digest in immutable.items():
            if path == plan_path:
                raw = SAFETY._read(path)
            else:
                _plain_directory(path.parent)
                raw = _regular_bytes(path, _reader=_reader)
            _need(_sha(raw) == digest, "INPUT_CHANGED", "sealed input changed")
        _need(not stop_launches.is_set(), "CHILD_FAILED", "no new calls after an uncertain turn")
        _need(cancel_event is None or not cancel_event.is_set(), "CANCELLED", "stop new calls; preserve existing runs")

    def make_request(actor: str, phase: str, round_number: int, topic_ids: list[str] | None = None) -> dict[str, Any]:
        web = phase == "research" or (phase == "initial" and actor in WEB_ACTORS)
        messages = [] if web else [row for row in history if row["id"] > (0 if phase in {"close", "synthesize"} else cursors[actor])
                                           and row["action"] not in {"pass", "agree"}]
        return {
            "actor": actor, "phase": phase, "round": round_number, "web_research": web,
            "public_brief": copy.deepcopy(plan["public_brief"]),
            "topic_ids": topic_ids or (list(plan["public_brief"]["topics"]) if web else []),
            "task": plan["public_brief"]["summary"] if web else plan["task"],
            "messages": copy.deepcopy(messages), "seen_through": 0 if web else len(history),
            "private_notes": "" if web else notes.get(actor, ""),
            "open_objections": [] if web else copy.deepcopy(list(objections.values())),
        }

    def invoke(request: dict[str, Any]) -> dict[str, Any]:
        if stop_launches.is_set() or (cancel_event is not None and cancel_event.is_set()):
            raise MeetingError("CANCELLED", "reserved turn was not admitted")
        try:
            response = provider(copy.deepcopy(request))
            _need(isinstance(response, dict) and set(response) == {"body", "provenance"}, "PROVIDER_INVALID", "provider envelope")
            _need(isinstance(response["provenance"], dict), "PROVIDER_INVALID", "provenance object required")
            return copy.deepcopy(response)
        except Exception:
            stop_launches.set()
            raise

    def accept(request: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        check()
        body = _validate_body(response["body"], request, objections)
        response_path = output / "turns" / request["turn_id"] / "response.json"
        SAFETY._immutable_bytes(response_path, _bytes(response), root)
        immutable[response_path] = _sha(response_path.read_bytes())
        if not simulation:
            proof = response["provenance"]
            _need(proof.get("kind") == "oracle_terminal" and proof.get("verified") is True,
                  "PROVENANCE_INVALID", "native terminal binding is mandatory")
            _need(not any(row["conversation_url"] == proof["conversation_url"] or row["slug"] == proof["slug"]
                          for row in result["sessions"]), "SHARED_SESSION", "every turn must be independent")
            result["sessions"].append(copy.deepcopy(proof))
            result["verified_web_session_count"] += 1
        message = {"id": len(history) + 1, "actor": request["actor"], "phase": request["phase"],
                   "round": request["round"], **body, "turn_id": request["turn_id"],
                   "provenance": response["provenance"], "simulation": simulation}
        store.append("message", message)
        history.append(message)
        if not request["web_research"]:
            cursors[request["actor"]] = request["seen_through"]
            notes[request["actor"]] = body["text"]
        if body["action"] == "object":
            objections[message["id"]] = {"id": message["id"], "actor": message["actor"], "text": message["text"],
                                          "reply_to": message["reply_to"]}
        for identifier in body["resolves"]:
            objections.pop(identifier)
        if request["phase"] == "initial" and request["web_research"] and body["action"] == "claim" and body["evidence"]:
            pending_initial_research.discard(request["actor"])
        if body["action"] == "research":
            requested.update(body["topic_ids"])
        for card in body["evidence"]:
            source = sources.setdefault(card["url"], {"id": "source-" + _sha(card["url"].encode())[:20],
                                                        "url": card["url"], "claims": [], "verification": "agent_reported_unverified"})
            source["claims"].append({**card, "actor": request["actor"], "message_id": message["id"]})
        return message

    def wave(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
        accepted = []
        for start in range(0, len(requests), plan["max_concurrency"]):
            check()
            batch = requests[start:start + plan["max_concurrency"]]
            _need(result["launch_attempt_count"] + len(batch) <= plan["max_calls"], "CALL_BUDGET", "provider budget exhausted")
            for request in batch:
                result["launch_attempt_count"] += 1
                request["turn_id"] = f"turn-{result['launch_attempt_count']:03d}-{request['actor']}"
                raw = _bytes(request)
                _need(len(raw) <= MAX_BYTES, "CONTEXT_BUDGET", "do not silently truncate the meeting")
                path = output / "turns" / request["turn_id"] / "request.json"
                SAFETY._immutable_bytes(path, raw, root)
                immutable[path] = _sha(raw)
                store.append("reserved", {"turn_id": request["turn_id"], "actor": request["actor"], "phase": request["phase"],
                                           "request_sha256": _sha(raw), "request_path": str(path), "submission_proven": False})
            with ThreadPoolExecutor(max_workers=len(batch)) as pool:
                futures = [pool.submit(invoke, request) for request in batch]
                responses = []
                errors = []
                for request, future in zip(batch, futures, strict=True):
                    try:
                        responses.append((request, future.result()))
                    except Exception as exc:
                        errors.append(exc)
                if errors:
                    raise errors[0]
                for request, response in responses:
                    accepted.append(accept(request, response))
        return accepted

    try:
        wave([make_request(actor, "initial", 0) for actor in ACTORS])
        final_reserve = len(ACTORS) + 1
        for number in range(1, plan["review_rounds"] + 1):
            if result["launch_attempt_count"] + len(ACTORS) + final_reserve > plan["max_calls"]:
                break
            wave([make_request(actor, "react", number) for actor in ACTORS])
            result["review_rounds_completed"] = number
            pending = sorted(requested - research_attempted)
            for topic in pending:
                if result["research_jobs"] >= plan["max_research_jobs"] or result["launch_attempt_count"] + 1 + final_reserve > plan["max_calls"]:
                    break
                research_attempted.add(topic)
                result["research_jobs"] += 1
                research_result = wave([make_request("researcher", "research", number, [topic])])[0]
                if research_result["action"] == "claim" and research_result["evidence"]:
                    researched.add(topic)
                    result["completed_research_jobs"] += 1
        # Everyone sees the SAME decision snapshot; a new substantive final
        # objection/revision invalidates earlier agreements instead of counting silence.
        closing = wave([make_request(actor, "close", plan["review_rounds"] + 1) for actor in ACTORS])
        result["reviews"] = {row["actor"]: row["action"] for row in closing}
        result["closing_snapshot_stale"] = any(row["resolves"] or row["action"] not in {"agree", "pass"} for row in closing)
        result["consensus_reached"] = (all(row["action"] == "agree" for row in closing)
                                        and not result["closing_snapshot_stale"]
                                        and not objections and not (requested - researched)
                                        and not pending_initial_research)
        synthesis_request = make_request("synthesizer", "synthesize", plan["review_rounds"] + 1)
        synthesis_request["decision"] = {"consensus_reached": result["consensus_reached"], "reviews": result["reviews"],
                                          "pending_research": sorted(requested - researched),
                                          "pending_initial_research": sorted(pending_initial_research), "solution_verified": False}
        synthesis = wave([synthesis_request])[0]
        result.update(synthesis=synthesis["text"], synthesis_message_id=synthesis["id"], workflow_complete=True,
                      ok=result["consensus_reached"], status="complete" if result["consensus_reached"] else "inconclusive")
        check()
        store.append("finished", {"status": result["status"], "consensus_reached": result["consensus_reached"],
                                   "solution_verified": False, "simulation": simulation})
    except Exception as exc:
        stop_launches.set()
        code = getattr(exc, "code", "MEETING_FAILED")
        result.update(ok=False, consensus_reached=False, workflow_complete=False,
                      status="cancelled" if code == "CANCELLED" else "attention_required",
                      error={"code": code, "message": str(exc)})
        # Preserve tampered evidence. Never rewrite a broken chain to make it pass.
    finally:
        stop_launches.set()
    result.update(open_objections=list(objections.values()), pending_research=sorted(requested - researched),
                  pending_initial_research=sorted(pending_initial_research),
                  evidence_sources=list(sources.values()), event_count=len(store.hashes),
                  event_tail_sha256=store.hashes[-1], input_sha256={str(path): digest for path, digest in immutable.items()})
    _publish(output / "result.json", _bytes(result), root)
    return result


def _display(text: str) -> str:
    return "".join(character if character in "\n\t" or not unicodedata.category(character).startswith("C")
                   else f"\\u{ord(character):04x}" for character in text)


def _render_snapshot(directory: Path, *, after: int = 0) -> tuple[str, int]:
    events = read_events(directory)
    result_path = directory.parent / "result.json"
    if result_path.exists():
        result = json.loads(_regular_bytes(SAFETY._safe_path(result_path), limit=4 * 1024 * 1024))
        if type(result.get("event_count")) is int and result["event_count"] > len(events):
            # Completion may have published between the first snapshot and the
            # result read. Refresh once; never advance a cursor past unseen data.
            refreshed = read_events(directory)
            _need(refreshed[:len(events)] == events, "EVENT_LOG_CHANGED", "published prefix changed")
            events = refreshed
        _need(result.get("event_count") == len(events) and events
              and result.get("event_tail_sha256") == events[-1]["sha256"], "EVENT_LOG_CHANGED", "final anchor mismatch")
    lines = []
    for event in events[after:]:
        data = event["data"]
        if event["kind"] == "message":
            label = "SIMULATION" if data["simulation"] else "ORACLE"
            lines.append(f"[{event['sequence']:04d}] [{label}] {data['actor']} / {data['action']} / reply={data['reply_to']}")
            lines.append(_display(data["text"]))
        elif event["kind"] in {"started", "finished"}:
            lines.append(_display(f"[{event['sequence']:04d}] {event['kind']}: {json.dumps(data, ensure_ascii=False)}"))
    return "\n".join(lines), len(events)


def render_events(directory: Path, *, after: int = 0) -> str:
    return _render_snapshot(directory, after=after)[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Research meeting: plan, run exact approved bytes, or view agent events.")
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="write a reviewable plan; no providers or browser")
    plan.add_argument("--project-root", type=Path, required=True)
    plan.add_argument("--task-file", type=Path, required=True)
    plan.add_argument("--public-brief", type=Path, required=True, help="JSON with summary and approved topics")
    plan.add_argument("--public-root", type=Path, help="separate public-only, explicitly registered workspace for live research")
    plan.add_argument("--plan", type=Path, required=True)
    plan.add_argument("--max-calls", type=int, default=24)
    plan.add_argument("--review-rounds", type=int, default=2)
    plan.add_argument("--max-research-jobs", type=int, default=3)
    plan.add_argument("--max-concurrency", type=int, default=2)
    run = commands.add_parser("run", help="live Oracle execution; requires native task identity and admission")
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--expected-plan-sha256", required=True)
    view = commands.add_parser("view", help="read-only terminal meeting log")
    view.add_argument("--events-dir", type=Path, required=True)
    view.add_argument("--follow-seconds", type=int, default=0, choices=range(0, 3601), metavar="0..3600")
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            path = write_plan(args.plan, project_root=args.project_root,
                              task=args.task_file.read_text(encoding="utf-8"), public_brief=_json(args.public_brief.read_bytes()),
                              public_root=args.public_root, max_calls=args.max_calls, review_rounds=args.review_rounds,
                              max_research_jobs=args.max_research_jobs, max_concurrency=args.max_concurrency)
            print(json.dumps({"status": "plan-only", "plan_path": str(path), "sha256": _sha(path.read_bytes()),
                              "submitted": False}, ensure_ascii=True))
            return 0
        if args.command == "view":
            deadline, count = time.monotonic() + args.follow_seconds, 0
            while True:
                text, count = _render_snapshot(args.events_dir, after=count)
                if text:
                    print(text, flush=True)
                if time.monotonic() >= deadline or (args.events_dir.parent / "result.json").exists():
                    return 0
                time.sleep(0.5)
        result = run_meeting(args.plan, expected_sha256=args.expected_plan_sha256)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0 if result["ok"] else 2
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": {"code": getattr(exc, "code", "MEETING_ERROR"), "message": str(exc)}}, ensure_ascii=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
