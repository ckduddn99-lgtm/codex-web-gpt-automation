from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "bin" / "cloudflare_telegram_breakglass_worker.js"
WORKFLOW = ROOT / ".github" / "workflows" / "telegram-breakglass.yml"


def test_worker_requires_telegram_secret_and_exact_identity():
    text = WORKER.read_text(encoding="utf-8")
    assert "x-telegram-bot-api-secret-token" in text
    assert "TELEGRAM_ALLOWED_CHAT_ID" in text
    assert "TELEGRAM_ALLOWED_USER_ID" in text
    assert "repository_dispatch" not in text
    assert "event_type: 'telegram-breakglass'" in text


def test_worker_allows_only_fixed_commands():
    text = WORKER.read_text(encoding="utf-8")
    for command in ("/heal", "/status", "/restart_pc", "/stop_browser"):
        assert command in text
    assert "ALLOWED_COMMANDS" in text


def test_workflow_is_bounded_and_uses_ephemeral_ssh_secret():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "timeout-minutes: 5" in text
    assert "BREAKGLASS_SSH_PRIVATE_KEY" in text
    assert "AGENT_BOX_KNOWN_HOSTS" in text
    assert "StrictHostKeyChecking=yes" in text
    assert "BREAKGLASS_TARGET_HOST" in text
    assert "BREAKGLASS_TARGET_USER" in text
    assert "34.53.1.177" not in text
