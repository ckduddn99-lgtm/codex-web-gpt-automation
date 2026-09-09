from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "desktop_commander_compat.py"
spec = importlib.util.spec_from_file_location("desktop_commander_compat_test", MODULE_PATH)
assert spec is not None and spec.loader is not None
compat = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = compat
spec.loader.exec_module(compat)


def _package(tmp_path: Path, source: str, *, version: str = compat.SUPPORTED_VERSION) -> Path:
    root = tmp_path / "app"
    target = root / compat.DEVICE_RELATIVE_PATH
    target.parent.mkdir(parents=True)
    target.write_text(source, encoding="utf-8")
    (root / "package.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    return root


def _pristine_fixture() -> str:
    return """import os from 'os';
import fs from 'fs/promises';
import path from 'path';
export class MCPDevice {
    constructor(options = {}) {
        this.remoteChannel = new RemoteChannel();
        this.deviceId = undefined;
        this.isShuttingDown = false;
        this.configPath = path.join(os.homedir(), '.desktop-commander-device', 'device.json');
        this.persistSession = options.persistSession ?? true;
    }
    async start() {
            // Initialize Remote Channel
            this.remoteChannel.initialize(supabaseUrl, anonKey);
            // Load persisted configuration (deviceId, session)
    }
    async savePersistedConfig() {
        try {
            console.debug('[DEBUG] Saving persisted config, persistSession:', this.persistSession);
            const currentSessionStore = await this.remoteChannel.getSession();
            const session = currentSessionStore.data.session;
            const config = {
                deviceId: this.deviceId,
                // Only save session if --persist-session flag is set
                session: (session && this.persistSession) ? {
                    access_token: session.access_token,
                    refresh_token: session.refresh_token
                } : null
            };
            // Ensure the config directory exists
            console.debug('[DEBUG] Creating config directory:', path.dirname(this.configPath));
            await fs.mkdir(path.dirname(this.configPath), { recursive: true });
            await fs.writeFile(this.configPath, JSON.stringify(config, null, 2), { mode: 0o600 });
            console.debug('[DEBUG] Config saved to:', this.configPath);
        }
        catch (error) {
            console.error(' - ❌ Failed to save config:', error.message);
            console.debug('[DEBUG] Config save error details:', error);
            await captureRemote('remote_device_config_save_error', { error });
        }
    }
}
"""


def test_transform_persists_rotated_refresh_tokens_atomically() -> None:
    patched = compat.transform_source(_pristine_fixture())
    assert "event !== 'TOKEN_REFRESHED'" in patched
    assert "persistRefreshedSession(newSession)" in patched
    assert "this.sessionPersistChain = Promise.resolve();" in patched
    assert "await fs.rename(tempPath, this.configPath);" in patched
    assert "refresh_token: session.refresh_token" in patched


def test_transform_fails_closed_when_patch_context_drifts() -> None:
    with pytest.raises(compat.DesktopCommanderCompatError) as caught:
        compat.transform_source(_pristine_fixture().replace("this.deviceId = undefined;", "this.deviceId = null;"))
    assert caught.value.code == "COMMANDER_PATCH_CONTEXT_MISMATCH"


def test_inspect_rejects_unsupported_version(tmp_path: Path) -> None:
    root = _package(tmp_path, _pristine_fixture(), version="9.9.9")
    info = compat.inspect_package(root)
    assert info["state"] == "unsupported-version"


def test_apply_rejects_unknown_build(tmp_path: Path) -> None:
    root = _package(tmp_path, _pristine_fixture())
    with pytest.raises(compat.DesktopCommanderCompatError) as caught:
        compat.apply_patch(root)
    assert caught.value.code == "COMMANDER_BUILD_UNSUPPORTED"
