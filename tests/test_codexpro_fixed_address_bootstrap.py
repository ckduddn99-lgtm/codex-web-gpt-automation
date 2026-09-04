from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


CODEX_HOME = Path(__file__).resolve().parents[1]
BOOTSTRAP = CODEX_HOME / "bin" / "codexpro_project_cloudflare_bootstrap.ps1"
WATCHDOG = CODEX_HOME / "bin" / "codexpro_fixed_runtime_watchdog.py"


def _source() -> str:
    return BOOTSTRAP.read_text(encoding="utf-8")


def _dry_run_process(tmp_path: Path, decision: dict, *args: str) -> subprocess.CompletedProcess[str]:
    if shutil.which("powershell.exe") is None:
        pytest.skip("Windows exact-unit bootstrap requires PowerShell")
    user_profile = tmp_path / "profile"
    bin_dir = user_profile / ".codex" / "bin"
    bin_dir.mkdir(parents=True)
    bootstrap = bin_dir / BOOTSTRAP.name
    shutil.copy2(BOOTSTRAP, bootstrap)
    bootstrap_source = bootstrap.read_text(encoding="utf-8")
    manager_invocation = "$decision = Invoke-ManagerJson -ManagerArgs $decisionArgs"
    assert bootstrap_source.count(manager_invocation) == 1
    bootstrap.write_text(
        bootstrap_source.replace(manager_invocation, '$decision = $env:CODEXPRO_TEST_DECISION_JSON | ConvertFrom-Json'),
        encoding="utf-8",
    )
    (bin_dir / "codexpro_project_app_manager.py").write_text(
        "import os\nprint(os.environ['CODEXPRO_TEST_DECISION_JSON'])\n",
        encoding="utf-8",
    )
    (bin_dir / "codexpro_mcp_identity.py").write_text("# dry-run identity stub\n", encoding="utf-8")
    state_dir = user_profile / ".codex" / "state" / "codexpro-project-apps"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "drive-tunnel-policy.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "default_provider": "cloudflare",
                "drives": {
                    "C:\\": {
                        "provider": "ngrok",
                        "hostname": "fixed.example.test",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    fake_npx = tmp_path / "npx.cmd"
    fake_npx.write_text("@echo off\r\n", encoding="utf-8")
    fake_ngrok = tmp_path / "ngrok.cmd"
    fake_ngrok.write_text("@echo off\r\n", encoding="utf-8")
    env = os.environ.copy()
    env["USERPROFILE"] = str(user_profile)
    env["CODEXPRO_TEST_DECISION_JSON"] = json.dumps(decision)
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(bootstrap),
            "-NpxPath",
            str(fake_npx),
            "-NgrokPath",
            str(fake_ngrok),
            "-DryRun",
            *args,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        # PowerShell 5.1 writes its error records in the console code page, so a
        # non-ASCII path leaks bytes that are not valid UTF-8.  Decoding strictly
        # kills the reader thread and leaves stderr as None; the contract tokens
        # asserted below are ASCII and survive replacement.  Same handling as
        # test_install_lifecycle.run_powershell.
        errors="replace",
        env=env,
    )
    return completed


def _dry_run(tmp_path: Path, decision: dict, *args: str) -> dict:
    completed = _dry_run_process(tmp_path, decision, *args)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_bootstrap_is_valid_powershell() -> None:
    if shutil.which("powershell.exe") is None:
        pytest.skip("Windows exact-unit bootstrap requires PowerShell")
    command = (
        "$tokens=$null; $errors=$null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{BOOTSTRAP}',"
        "[ref]$tokens,[ref]$errors) | Out-Null; "
        "if($errors.Count){$errors | ForEach-Object { Write-Error $_ }; exit 1}"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stderr


def test_fixed_ngrok_recovery_starts_only_the_local_codexpro_server() -> None:
    source = _source()
    assert "Get-ExistingFixedNgrokProcess" in source
    assert '"--tunnel", "none", "--token", $Token' in source
    assert 'launchMode = "reuse-fixed-tunnel-start-local-server-only"' in source
    assert "do not launch a second ngrok tunnel" in source


def test_fixed_ngrok_ready_path_starts_singleton_runtime_watchdog() -> None:
    source = _source()
    assert "function Start-FixedRuntimeWatchdog" in source
    assert '"bin\\codexpro_fixed_runtime_watchdog.py"' in source
    assert "watchdog_start_requested" in source


def test_fixed_runtime_watchdog_repairs_listener_without_creating_tunnel() -> None:
    source = WATCHDOG.read_text(encoding="utf-8")
    assert '"-TunnelProvider",' in source
    assert '"auto",' in source
    assert "fixed_tunnel_present" in source
    assert "the exact fixed ngrok tunnel is absent" in source
    assert "ngrok http" not in source


def test_registered_runtime_url_keeps_the_query_delimiter_and_token() -> None:
    source = _source()
    assert '"${runtimeEndpoint}?codexpro_token=$Token"' in source
    assert '"${runtimeEndpoint}?codexpro_token=$($tokenMatch.Groups[1].Value)"' in source
    assert '"$runtimeEndpoint?codexpro_token=$Token"' not in source


def test_listener_check_is_bounded_and_identity_mismatch_fails_closed() -> None:
    source = _source()
    assert "function Test-LocalTcpPort" in source
    assert "Test-NetConnection" not in source
    assert 'status = "port-occupied-identity-mismatch"' in source
    assert 'action = "blocked-without-starting-a-duplicate-runtime"' in source



def test_complete_fixed_registry_contract_keeps_ngrok_as_effective_provider(tmp_path: Path) -> None:
    result = _dry_run(
        tmp_path,
        {
            "root": "C:\\",
            "slug": "CDrive",
            "app_name": "CodexPro-CDrive-v11",
            "version": 11,
            "port": 8790,
            "public_url": "https://fixed.example.test/mcp?codexpro_token=[REDACTED_SECRET]",
            "action": "reuse",
            "old_app_name": None,
            "old_public_url": None,
            "chrome_next_action": "select-existing-app-refresh-if-hidden",
            "transaction_id": None,
        },
        "-Root",
        "C:\\",
        "-Port",
        "8790",
        "-TunnelProvider",
        "ngrok",
        "-Hostname",
        "fixed.example.test",
        "-Token",
        "[REDACTED_SECRET]",
    )

    assert result["fixed_ngrok_registry_contract"] is True
    assert result["effective_tunnel_provider"] == "ngrok"
    assert result["effective_hostname"] == "fixed.example.test"
    assert result["public_identity_required"] is True


def test_other_drive_uses_dynamic_cloudflare_policy(tmp_path: Path) -> None:
    result = _dry_run(
        tmp_path,
        {
            "root": "D:\\",
            "slug": "DDrive",
            "app_name": "CodexPro-DDrive-v01",
            "version": 1,
            "port": 8794,
            "public_url": None,
            "action": "create",
            "old_app_name": None,
            "old_public_url": None,
            "chrome_next_action": "create-new-app",
            "transaction_id": None,
        },
        "-Root",
        "D:\\",
        "-Port",
        "8794",
    )

    assert result["fixed_ngrok_registry_contract"] is False
    assert result["fixed_ngrok_contract_reason"] == "dynamic-provider-requested"
    assert result["effective_tunnel_provider"] == "cloudflare"
    assert result["effective_hostname"] == ""
    assert result["action"] == "create"


def test_fixed_registry_port_mismatch_blocks_without_dynamic_fallback(tmp_path: Path) -> None:
    completed = _dry_run_process(
        tmp_path,
        {
            "root": "C:\\",
            "slug": "CDrive",
            "app_name": "CodexPro-CDrive-v11",
            "version": 11,
            "port": 8790,
            "public_url": "https://fixed.example.test/mcp?codexpro_token=[REDACTED_SECRET]",
            "action": "reuse",
            "old_app_name": None,
            "old_public_url": None,
            "chrome_next_action": "select-existing-app-refresh-if-hidden",
            "transaction_id": None,
        },
        "-Root",
        "C:\\",
        "-Port",
        "8791",
        "-TunnelProvider",
        "ngrok",
        "-Hostname",
        "fixed.example.test",
        "-Token",
        "[REDACTED_SECRET]",
    )

    assert completed.returncode != 0
    # PowerShell may wrap a long Write-Error record at the host width.  The
    # contract token, rather than physical line layout, is the assertion.
    assert "FIXED_NGROK_CONTRACT_INVALID:fixed-port-registry-mismatch" in "".join(completed.stderr.split())


def test_same_drive_project_reuses_drive_root_fixed_registry_entry(tmp_path: Path) -> None:
    result = _dry_run(
        tmp_path,
        {
            "root": "C:\\",
            "slug": "CDrive",
            "app_name": "CodexPro-CDrive-v11",
            "version": 11,
            "port": 8790,
            "public_url": "https://fixed.example.test/mcp?codexpro_token=[REDACTED_SECRET]",
            "action": "reuse",
            "old_app_name": None,
            "old_public_url": None,
            "chrome_next_action": "select-existing-app-refresh-if-hidden",
            "transaction_id": None,
        },
        "-Root",
        "C:\\repo-b",
        "-Port",
        "8790",
        "-TunnelProvider",
        "ngrok",
        "-Hostname",
        "fixed.example.test",
        "-Token",
        "[REDACTED_SECRET]",
    )

    assert result["fixed_ngrok_registry_contract"] is True
    assert result["effective_tunnel_provider"] == "ngrok"
    assert result["root"] == "C:\\"


def test_cdrive_policy_rejects_explicit_cloudflare_override(tmp_path: Path) -> None:
    completed = _dry_run_process(
        tmp_path,
        {
            "root": "C:\\",
            "slug": "CDrive",
            "app_name": "CodexPro-CDrive-v11",
            "version": 11,
            "port": 8790,
            "public_url": "https://fixed.example.test/mcp?codexpro_token=[REDACTED_SECRET]",
            "action": "reuse",
            "old_app_name": None,
            "old_public_url": None,
            "chrome_next_action": "select-existing-app-refresh-if-hidden",
            "transaction_id": None,
        },
        "-Root",
        "C:\\repo-b",
        "-TunnelProvider",
        "cloudflare",
    )

    assert completed.returncode != 0
    assert "DRIVE_TUNNEL_POLICY_MISMATCH" in completed.stderr
