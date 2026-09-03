"""Mask credentials and conversation identifiers before they reach logs or ledgers.

Multi-agent runs print a per-worker report and persist `result.json`.  Both carry
session locators, run directories, and whatever a runner happened to attach, so a
leaked bearer token or session cookie would become a durable on-disk artifact.
Redaction happens at the reporting boundary rather than at capture time: the
runtime still needs the real locator to poll and recover a session, so the values
stay intact in memory and only the emitted copy is masked.
"""

from __future__ import annotations

import re
from typing import Any


REDACTION_SCHEMA = "codex.chatgpt.log-redaction/v1"
REDACTED = "[REDACTED]"

# Ordered longest-context-first so a broader pattern cannot swallow the tail of a
# narrower one and leave a recognizable fragment behind.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_.-]{4,}")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}")),
    ("cookie_pair", re.compile(r"(?i)\b((?:__Secure-|__Host-)?[A-Za-z0-9_.-]*(?:session|token|auth|secret|passwd|password)[A-Za-z0-9_.-]*)=([^;\s\"']{6,})")),
    ("conversation_uuid", re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")),
    ("long_hex", re.compile(r"\b[0-9a-fA-F]{40,}\b")),
)

# Keys whose entire value is sensitive regardless of shape.  Matching is on the
# normalized key name so `Cookie`, `cookie`, and `set-cookie` all qualify.
_SENSITIVE_KEY_PARTS = (
    "cookie",
    "token",
    "secret",
    "password",
    "passwd",
    "authorization",
    "api_key",
    "apikey",
    "credential",
)


def _key_is_sensitive(key: str) -> bool:
    normalized = key.replace("-", "_").casefold()
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def redact_text(value: str) -> str:
    """Mask every known secret shape in a single string."""
    if not value:
        return value
    masked = value
    for name, pattern in _PATTERNS:
        if name == "cookie_pair":
            masked = pattern.sub(lambda match: f"{match.group(1)}={REDACTED}", masked)
        else:
            masked = pattern.sub(REDACTED, masked)
    return masked


def redact_payload(value: Any) -> Any:
    """Return a redacted deep copy of a JSON-shaped payload.

    The input is never mutated: callers keep the live values they still need and
    emit only the returned copy.
    """
    if isinstance(value, dict):
        result: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _key_is_sensitive(key) and item is not None:
                result[key] = REDACTED if not isinstance(item, (dict, list)) else redact_payload(item)
            else:
                result[key] = redact_payload(item)
        return result
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_payload(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value
