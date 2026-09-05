"""Run-scoped I/O reuse must never cache artifact contents or validation results."""
from __future__ import annotations

from collections import Counter
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "meeting_io_fixtures", Path(__file__).with_name("test_chatgpt_research_meeting.py")
)
FIXTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURE)


def observe_event_handles(module, monkeypatch):
    original = module.Path.open
    calls = Counter()
    handles = []

    def observed(path, mode="r", *args, **kwargs):
        stream = original(path, mode, *args, **kwargs)
        if path.parent.name == "events" and mode == "rb":
            calls[path] += 1
            handles.append(stream)
        return stream

    monkeypatch.setattr(module.Path, "open", observed)
    return calls, handles


def test_controller_reuses_event_handles_without_reopening_each_prefix(tmp_path, monkeypatch):
    module = FIXTURE.load()
    calls, handles = observe_event_handles(module, monkeypatch)
    result = FIXTURE.run(module, FIXTURE.plan(module, tmp_path, max_calls=9))
    assert result["status"] == "complete"
    assert calls and len(calls) == result["event_count"] - 1
    assert max(calls.values()) == 1, "same event must be re-read, not repeatedly reopened"
    assert all(stream.closed for stream in handles), "terminal return must close every retained handle"


def test_reused_event_handle_detects_same_size_same_mtime_content_tampering(tmp_path, monkeypatch):
    module = FIXTURE.load()
    _, handles = observe_event_handles(module, monkeypatch)
    path = FIXTURE.plan(module, tmp_path, max_calls=9, max_concurrency=1)

    def tamper(request, body):
        if request["phase"] == "initial" and request["actor"] == "analyst":
            target = path.parent / "run/events/000001.json"
            before = target.stat()
            raw = target.read_bytes()
            changed = raw.replace(b"started", b"changed")
            assert len(raw) == len(changed) and raw != changed
            target.write_bytes(changed)
            os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))

    result = FIXTURE.run(module, path, FIXTURE.Provider(tamper))
    assert result["status"] == "attention_required"
    assert result["error"]["code"] == "EVENT_LOG_CHANGED"
    assert result["launch_attempt_count"] == 1
    assert handles and all(stream.closed for stream in handles)


@pytest.mark.parametrize("mutation", ["reparse", "inode"])
def test_reused_handle_revalidates_the_current_path_identity(tmp_path, monkeypatch, mutation):
    module = FIXTURE.load()
    _, handles = observe_event_handles(module, monkeypatch)
    path = FIXTURE.plan(module, tmp_path, max_calls=9, max_concurrency=1)
    target = path.parent / "run/events/000001.json"
    original = module.Path.lstat
    changed = False

    def current_identity(candidate, *args, **kwargs):
        value = original(candidate, *args, **kwargs)
        if changed and candidate == target:
            return SimpleNamespace(
                st_mode=value.st_mode, st_dev=value.st_dev,
                st_ino=value.st_ino + (mutation == "inode"),
                st_file_attributes=getattr(value, "st_file_attributes", 0) | (0x400 if mutation == "reparse" else 0),
            )
        return value

    def tamper(request, body):
        nonlocal changed
        if request["phase"] == "initial" and request["actor"] == "analyst":
            changed = True

    monkeypatch.setattr(module.Path, "lstat", current_identity)
    result = FIXTURE.run(module, path, FIXTURE.Provider(tamper))
    assert result["status"] == "attention_required"
    assert result["error"]["code"] == "UNSAFE_PATH"
    assert result["launch_attempt_count"] == 1
    assert handles and all(stream.closed for stream in handles)


def test_reused_request_handle_still_rehashes_old_inputs_before_new_admission(tmp_path, monkeypatch):
    module = FIXTURE.load()
    _, handles = observe_event_handles(module, monkeypatch)
    path = FIXTURE.plan(module, tmp_path, review_rounds=1, max_calls=13, max_concurrency=1)

    def tamper(request, body):
        if request["phase"] == "react" and request["actor"] == "analyst":
            target = path.parent / "run/turns/turn-001-analyst/request.json"
            before = target.stat()
            raw = target.read_bytes()
            changed = raw.replace(b"PRIVATE", b"CHANGED")
            assert len(raw) == len(changed) and raw != changed
            target.write_bytes(changed)
            os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))

    provider = FIXTURE.Provider(tamper)
    result = FIXTURE.run(module, path, provider)
    assert result["status"] == "attention_required"
    assert result["error"]["code"] == "INPUT_CHANGED"
    assert result["launch_attempt_count"] == 5
    assert not any(request["phase"] == "close" for request in provider.requests)
    assert handles and all(stream.closed for stream in handles)


def test_handle_budget_is_bounded_and_all_handles_close(monkeypatch, tmp_path):
    from contextlib import ExitStack
    from io import BytesIO

    module = FIXTURE.load()
    opened = []

    def open_handle(path, mode, *, buffering):
        assert mode == "rb" and buffering == 0
        stream = BytesIO(b"unbuffered-open stand-in")
        opened.append(stream)
        return stream

    monkeypatch.setattr(module.Path, "open", open_handle)
    with ExitStack() as stack:
        reader = module._ArtifactReader(stack)
        for index in range(162):
            reader.open(tmp_path / f"artifact-{index}.json")
        with pytest.raises(module.MeetingError, match="ARTIFACT_LIMIT"):
            reader.open(tmp_path / "overflow.json")
        assert len(opened) == 162
    assert all(stream.closed for stream in opened)


def test_result_publication_failure_also_closes_retained_handles(tmp_path, monkeypatch):
    module = FIXTURE.load()
    _, handles = observe_event_handles(module, monkeypatch)
    publish = module._publish

    def fail_result(path, *args, **kwargs):
        if path.name == "result.json":
            raise OSError("synthetic result publication failure")
        return publish(path, *args, **kwargs)

    monkeypatch.setattr(module, "_publish", fail_result)
    with pytest.raises(OSError, match="synthetic result publication failure"):
        FIXTURE.run(module, FIXTURE.plan(module, tmp_path, max_calls=9))
    assert handles and all(stream.closed for stream in handles)


def test_failed_child_closes_retained_handles_without_admitting_a_replacement(tmp_path, monkeypatch):
    module = FIXTURE.load()
    _, handles = observe_event_handles(module, monkeypatch)

    def fail(request, body):
        raise RuntimeError("synthetic uncertain child")

    result = FIXTURE.run(
        module, FIXTURE.plan(module, tmp_path, max_calls=9, max_concurrency=1), FIXTURE.Provider(fail)
    )
    assert result["status"] == "attention_required"
    assert result["launch_attempt_count"] == 1
    assert not result["auto_retry"]
    assert handles and all(stream.closed for stream in handles)
