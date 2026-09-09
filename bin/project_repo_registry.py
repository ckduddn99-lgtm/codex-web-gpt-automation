from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPOS = {
    "automation": REPO_ROOT,
    "stock": Path("/home/ckduddn99/stock-ai-app"),
}


class RepoRegistryError(RuntimeError):
    pass


def normalize_repo_id(value: str) -> str:
    text = str(value or "").strip().casefold()
    if not text or len(text) > 64 or not text[0].isalnum():
        raise RepoRegistryError("repo id must be 1-64 safe ASCII characters")
    if any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for ch in text):
        raise RepoRegistryError("repo id must be 1-64 safe ASCII characters")
    return text


def load_registry(items: Sequence[str] = (), *, environ: Mapping[str, str] | None = None) -> dict[str, Path]:
    env = os.environ if environ is None else environ
    registry = dict(DEFAULT_REPOS)
    raw = str(env.get("PROJECT_CONTROL_REPOS_JSON") or "").strip()
    if raw:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RepoRegistryError("PROJECT_CONTROL_REPOS_JSON must be a JSON object") from exc
        if not isinstance(value, dict):
            raise RepoRegistryError("PROJECT_CONTROL_REPOS_JSON must be a JSON object")
        for name, path in value.items():
            registry[normalize_repo_id(str(name))] = Path(str(path)).expanduser()
    env_routes = str(env.get("PROJECT_CONTROL_REPO_ROUTES") or "").strip()
    merged_items = [part.strip() for part in env_routes.split(",") if part.strip()]
    merged_items.extend(items)
    for item in merged_items:
        name, sep, value = str(item).partition("=")
        if not sep or not name.strip() or not value.strip():
            raise RepoRegistryError("repo route must be ID=PATH")
        registry[normalize_repo_id(name)] = Path(value).expanduser()
    return registry
