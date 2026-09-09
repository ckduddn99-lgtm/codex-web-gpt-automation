from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

SUPPORTED_VERSION = "0.2.48"
DEVICE_RELATIVE_PATH = Path("dist/remote-device/device.js")
PRISTINE_SHA256 = "02cd9f6cdfa65a63c93fe709675499e2d0a265561d277078f7201c4d540a2509"
PATCHED_SHA256 = "842fcb2f5830fa47849dd34d0479216fec7c00c82f6c57727089971fdfad4ea4"

CONSTRUCTOR_OLD = """        this.remoteChannel = new RemoteChannel();
        this.deviceId = undefined;
        this.isShuttingDown = false;
        this.configPath = path.join(os.homedir(), '.desktop-commander-device', 'device.json');
"""
CONSTRUCTOR_NEW = """        this.remoteChannel = new RemoteChannel();
        this.deviceId = undefined;
        this.isShuttingDown = false;
        this.configPath = path.join(os.homedir(), '.desktop-commander-device', 'device.json');
        // Serialize token snapshots so a rotated refresh token can never be
        // overwritten by an older in-flight config write.
        this.sessionPersistChain = Promise.resolve();
"""

INITIALIZE_OLD = """            // Initialize Remote Channel
            this.remoteChannel.initialize(supabaseUrl, anonKey);
            // Load persisted configuration (deviceId, session)
"""
INITIALIZE_NEW = """            // Initialize Remote Channel
            this.remoteChannel.initialize(supabaseUrl, anonKey);
            // Supabase refresh tokens rotate. Persist every TOKEN_REFRESHED
            // snapshot immediately; otherwise a later process restart replays
            // the already-consumed refresh token from device.json.
            this.remoteChannel.client?.auth.onAuthStateChange((event, newSession) => {
                if (event !== 'TOKEN_REFRESHED' || !this.persistSession ||
                    !newSession?.access_token || !newSession?.refresh_token) {
                    return;
                }
                void this.persistRefreshedSession(newSession);
            });
            // Load persisted configuration (deviceId, session)
"""

SAVE_OLD = """    async savePersistedConfig() {
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
"""
SAVE_NEW = """    queuePersistedConfig(session) {
        const config = {
            deviceId: this.deviceId,
            session: (session && this.persistSession) ? {
                access_token: session.access_token,
                refresh_token: session.refresh_token
            } : null
        };
        const tempPath = `${this.configPath}.${process.pid}.tmp`;
        this.sessionPersistChain = this.sessionPersistChain
            .catch(() => { })
            .then(async () => {
                await fs.mkdir(path.dirname(this.configPath), { recursive: true });
                await fs.writeFile(tempPath, JSON.stringify(config, null, 2), { mode: 0o600 });
                await fs.rename(tempPath, this.configPath);
            });
        return this.sessionPersistChain;
    }
    async persistRefreshedSession(session) {
        try {
            await this.queuePersistedConfig(session);
            console.debug('[DEBUG] Rotated remote session persisted');
        }
        catch (error) {
            console.error(' - ❌ Failed to persist rotated remote session:', error.message);
            await captureRemote('remote_device_config_save_error', { error });
        }
    }
    async savePersistedConfig() {
        try {
            console.debug('[DEBUG] Saving persisted config, persistSession:', this.persistSession);
            const currentSessionStore = await this.remoteChannel.getSession();
            await this.queuePersistedConfig(currentSessionStore.data.session);
            console.debug('[DEBUG] Config saved to:', this.configPath);
        }
        catch (error) {
            console.error(' - ❌ Failed to save config:', error.message);
            console.debug('[DEBUG] Config save error details:', error);
            await captureRemote('remote_device_config_save_error', { error });
        }
    }
"""

REPLACEMENTS = (
    (CONSTRUCTOR_OLD, CONSTRUCTOR_NEW),
    (INITIALIZE_OLD, INITIALIZE_NEW),
    (SAVE_OLD, SAVE_NEW),
)


class DesktopCommanderCompatError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def package_version(package_root: Path) -> str:
    try:
        metadata = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DesktopCommanderCompatError(
            "COMMANDER_PACKAGE_INVALID", "Desktop Commander package.json is unreadable"
        ) from exc
    return str(metadata.get("version") or "").strip()


def default_package_root() -> Path:
    override = str(os.environ.get("DESKTOP_COMMANDER_PACKAGE_ROOT") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".local/share/desktop-commander-bundle/app").resolve()


def transform_source(source: str) -> str:
    result = source
    for old, new in REPLACEMENTS:
        count = result.count(old)
        if count != 1:
            raise DesktopCommanderCompatError(
                "COMMANDER_PATCH_CONTEXT_MISMATCH",
                "Desktop Commander source no longer matches the tested patch context",
                {"matches": count, "context": old.splitlines()[0].strip()},
            )
        result = result.replace(old, new, 1)
    return result


def inspect_package(package_root: Path) -> dict[str, Any]:
    root = package_root.expanduser().resolve()
    version = package_version(root)
    target = root / DEVICE_RELATIVE_PATH
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise DesktopCommanderCompatError(
            "COMMANDER_DEVICE_SOURCE_UNREADABLE", "Desktop Commander device.js is unreadable"
        ) from exc
    digest = sha256_bytes(raw)
    if version != SUPPORTED_VERSION:
        state = "unsupported-version"
    elif digest == PRISTINE_SHA256:
        state = "pristine"
    elif PATCHED_SHA256 != "TO_BE_FILLED" and digest == PATCHED_SHA256:
        state = "patched"
    else:
        state = "unknown-build"
    return {"root": str(root), "version": version, "target": str(target), "sha256": digest, "state": state}


def apply_patch(package_root: Path) -> dict[str, Any]:
    info = inspect_package(package_root)
    if info["version"] != SUPPORTED_VERSION:
        raise DesktopCommanderCompatError(
            "COMMANDER_VERSION_UNSUPPORTED",
            "refusing to patch an untested Desktop Commander version",
            info,
        )
    if info["state"] == "patched":
        return {**info, "changed": False}
    if info["state"] != "pristine":
        raise DesktopCommanderCompatError(
            "COMMANDER_BUILD_UNSUPPORTED",
            "refusing to patch an unknown Desktop Commander build",
            info,
        )
    target = Path(info["target"])
    original = target.read_text(encoding="utf-8")
    patched = transform_source(original)
    patched_bytes = patched.encode("utf-8")
    patched_hash = sha256_bytes(patched_bytes)
    if PATCHED_SHA256 == "TO_BE_FILLED" or patched_hash != PATCHED_SHA256:
        raise DesktopCommanderCompatError(
            "COMMANDER_PATCH_HASH_MISMATCH",
            "generated Desktop Commander patch does not match the tested hash",
            {"generated_sha256": patched_hash, "expected_sha256": PATCHED_SHA256},
        )
    mode = target.stat().st_mode & 0o777
    temp = target.with_name(f".{target.name}.{os.getpid()}.compat-tmp")
    try:
        temp.write_bytes(patched_bytes)
        os.chmod(temp, mode)
        os.replace(temp, target)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
    return {**inspect_package(package_root), "changed": True}


def _emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="Exact-build Desktop Commander compatibility guard")
    parser.add_argument("command", choices=("status", "apply"))
    parser.add_argument("--package-root", type=Path, default=default_package_root())
    args = parser.parse_args()
    try:
        if args.command == "status":
            _emit(inspect_package(args.package_root))
        else:
            _emit(apply_patch(args.package_root))
        return 0
    except DesktopCommanderCompatError as exc:
        _emit({"ok": False, "code": exc.code, "message": str(exc), "evidence": exc.evidence})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
