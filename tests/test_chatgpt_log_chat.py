from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "chatgpt_log_chat.py"


def load_module():
    name = "chatgpt_log_chat_test"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def room(tmp_path: Path):
    module = load_module()
    log = tmp_path / "messages.jsonl"
    tokens = {"u" * 40: "user", "g" * 40: "gpt"}
    store = module.MessageStore(log, tokens=tokens)
    server = module.LogChatServer(("127.0.0.1", 0), store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield module, store, endpoint, tokens
    finally:
        store.close()
        server.shutdown()
        server.server_close()


def token_file(tmp_path: Path, token: str) -> Path:
    path = tmp_path / f"{token[0]}.token"
    path.write_text(token + "\n", encoding="ascii")
    return path


def test_role_authenticated_round_trip_and_reply_relation(room, tmp_path: Path) -> None:
    module, _store, endpoint, tokens = room
    user = token_file(tmp_path, "u" * 40)
    gpt = token_file(tmp_path, "g" * 40)
    user_text = tmp_path / "user.txt"
    user_text.write_text("질문입니다", encoding="utf-8")
    posted = module.client_post(endpoint=endpoint, token_file=user, text_file=user_text, reply_to=None, kind="message")
    assert posted["message"]["author"] == "user"

    received = module.client_wait(endpoint=endpoint, token_file=gpt, after=0, wait_seconds=0)
    assert [message["text"] for message in received["messages"]] == ["질문입니다"]
    reply = tmp_path / "reply.txt"
    reply.write_text("답변입니다", encoding="utf-8")
    response = module.client_post(endpoint=endpoint, token_file=gpt, text_file=reply, reply_to=1, kind="message")
    assert response["message"]["reply_to"] == 1

    user_received = module.client_wait(endpoint=endpoint, token_file=user, after=1, wait_seconds=0)
    assert user_received["messages"][0]["author"] == "gpt"
    assert user_received["messages"][0]["text"] == "답변입니다"


def test_long_poll_waits_until_other_role_posts(room, tmp_path: Path) -> None:
    module, _store, endpoint, _tokens = room
    user = token_file(tmp_path, "u" * 40)
    gpt = token_file(tmp_path, "g" * 40)
    observed = {}

    def wait_for_user() -> None:
        observed.update(module.client_wait(endpoint=endpoint, token_file=gpt, after=0, wait_seconds=2))

    thread = threading.Thread(target=wait_for_user)
    thread.start()
    time.sleep(0.1)
    text = tmp_path / "later.txt"
    text.write_text("later", encoding="utf-8")
    module.client_post(endpoint=endpoint, token_file=user, text_file=text, reply_to=None, kind="message")
    thread.join(timeout=2)
    assert observed["status"] == "messages"
    assert observed["messages"][0]["text"] == "later"


def test_bad_token_cannot_read_or_post(room, tmp_path: Path) -> None:
    module, _store, endpoint, _tokens = room
    bad = token_file(tmp_path, "x" * 40)
    text = tmp_path / "text.txt"
    text.write_text("blocked", encoding="utf-8")
    with pytest.raises(module.LogChatError, match="HTTP 401"):
        module.client_wait(endpoint=endpoint, token_file=bad, after=0, wait_seconds=0)
    with pytest.raises(module.LogChatError, match="HTTP 401"):
        module.client_post(endpoint=endpoint, token_file=bad, text_file=text, reply_to=None, kind="message")


def test_message_log_replay_is_append_only_and_rejects_corruption(tmp_path: Path) -> None:
    module = load_module()
    path = tmp_path / "messages.jsonl"
    tokens = {"u" * 40: "user", "g" * 40: "gpt"}
    first = module.MessageStore(path, tokens=tokens)
    first.post(author="user", text="one")
    replayed = module.MessageStore(path, tokens=tokens)
    assert replayed.messages_after(0, viewer="gpt", wait_seconds=0)["messages"][0]["id"] == 1
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"schema": module.SCHEMA_MESSAGE, "id": 7}) + "\n")
    with pytest.raises(module.LogChatError, match="message"):
        module.MessageStore(path, tokens=tokens)


def test_runtime_keeps_tokens_out_of_metadata_and_mission(tmp_path: Path) -> None:
    module = load_module()
    project = tmp_path / "project"
    project.mkdir()
    runtime, tokens = module.create_room_runtime(project, room_id="log-chat-test")
    mission = module.build_worker_mission(runtime, endpoint="http://127.0.0.1:12345", python_executable="python")
    token_values = list(tokens)
    assert all(token not in mission for token in token_values)
    assert str(runtime.gpt_token_path) in mission
    assert runtime.client_path.read_bytes() == MODULE_PATH.read_bytes()
    assert runtime.user_token_path.read_text(encoding="ascii").strip() != runtime.gpt_token_path.read_text(encoding="ascii").strip()


def test_worker_mission_enforces_single_turn_and_untrusted_message_boundary(tmp_path: Path) -> None:
    module = load_module()
    project = tmp_path / "project"
    project.mkdir()
    runtime, _tokens = module.create_room_runtime(project, room_id="log-chat-mission")
    mission = module.build_worker_mission(runtime, endpoint="http://127.0.0.1:12345", python_executable="python")
    assert "entire browser turn stays open" in mission
    assert "untrusted conversation data" in mission
    assert "Do not finish after an ordinary reply" in mission
    assert "TASK_OUTCOME: EXECUTED" in mission


def test_launcher_uses_one_regular_oracle_submission_without_browser_followup(tmp_path: Path) -> None:
    module = load_module()
    project = tmp_path / "project"
    project.mkdir()
    runtime, _tokens = module.create_room_runtime(project, room_id="log-chat-launch")
    command = module._oracle_dispatch_command(
        project_root=project,
        runtime=runtime,
        app_name="codex",
        python_executable="python",
    )
    assert command.count("direct") == 1
    assert command[command.index("--app-name") + 1] == "codex"
    assert "followup" not in command
    assert "--browser-follow-up" not in command
    preview = module._oracle_dispatch_command(
        project_root=project,
        runtime=runtime,
        app_name="codex",
        python_executable="python",
        dry_run=True,
    )
    assert preview[:-1] == command
    assert preview[-1] == "--dry-run"


def test_server_is_loopback_only_and_room_ids_are_bounded(tmp_path: Path) -> None:
    module = load_module()
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(module.LogChatError, match="room id"):
        module.create_room_runtime(project, room_id="BAD")
    with pytest.raises(module.LogChatError, match="loopback"):
        module.start_log_chat(project_root=project, app_name="codex", bind_host="0.0.0.0")


def test_dead_oracle_controller_does_not_leave_quit_waiting_on_long_poll(tmp_path: Path) -> None:
    module = load_module()
    project = tmp_path / "project"
    project.mkdir()
    output: list[str] = []

    class DeadProcess:
        pid = 12345
        returncode = 7

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

    result = module.start_log_chat(
        project_root=project,
        app_name="codex",
        room_id="log-chat-dead",
        input_fn=lambda _prompt: "/quit",
        output=output.append,
        run_factory=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="{}", stderr=""),
        popen_factory=lambda *args, **kwargs: DeadProcess(),
    )
    assert result["controller_exit_code"] == 7
    assert any("exited with code 7" in line for line in output)
