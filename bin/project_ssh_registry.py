"""Registered break-glass host aliases for Project Control."""
from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping, Sequence

HOST_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SSH_TARGET_RE = re.compile(
    r"^(?P<user>[A-Za-z0-9._-]+)@(?P<host>[A-Za-z0-9._-]+)(?::(?P<port>[0-9]{1,5}))?$"
)

DEFAULT_HOSTS: dict[str, str] = {"agent-box": "local"}


class SSHRegistryError(ValueError):
    pass


def normalize_host_id(value: str) -> str:
    text = str(value or "").strip().lower()
    if not HOST_ID_RE.fullmatch(text):
        raise SSHRegistryError("host id must match [a-z0-9][a-z0-9._-]{0,63}")
    return text

def _parse_target(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        mode = str(value.get("mode") or "").strip().lower()
        if mode == "local":
            return {"mode": "local"}
        if mode == "ssh":
            target = str(value.get("target") or "").strip()
        else:
            raise SSHRegistryError("host mapping mode must be local or ssh")
    else:
        target = str(value or "").strip()
        if target == "local":
            return {"mode": "local"}
    match = SSH_TARGET_RE.fullmatch(target)
    if not match:
        raise SSHRegistryError("SSH target must be user@host or user@host:port")
    port = int(match.group("port") or 22)
    if not 1 <= port <= 65535:
        raise SSHRegistryError("SSH port must be between 1 and 65535")
    return {
        "mode": "ssh", "target": f"{match.group('user')}@{match.group('host')}",
        "user": match.group("user"), "host": match.group("host"), "port": port,
    }

def load_registry(items: Sequence[str] = ()) -> dict[str, dict[str, Any]]:
    registry = {normalize_host_id(k): _parse_target(v) for k, v in DEFAULT_HOSTS.items()}
    raw = os.environ.get("PROJECT_CONTROL_SSH_HOSTS_JSON", "").strip()
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SSHRegistryError("PROJECT_CONTROL_SSH_HOSTS_JSON must be valid JSON") from exc
        if not isinstance(data, Mapping):
            raise SSHRegistryError("PROJECT_CONTROL_SSH_HOSTS_JSON must be an object")
        for key, value in data.items():
            registry[normalize_host_id(key)] = _parse_target(value)
    for item in items:
        if "=" not in item:
            raise SSHRegistryError("SSH route must use ID=TARGET")
        key, value = item.split("=", 1)
        registry[normalize_host_id(key)] = _parse_target(value)
    return registry
