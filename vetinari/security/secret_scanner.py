"""Recursive secret scanning helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_SECRET_RES = (
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b", re.IGNORECASE),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b", re.IGNORECASE),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
)
_SENSITIVE_KEY_PARTS = frozenset({"password", "passwd", "secret", "token", "api_key", "apikey", "credential"})
_REDACTION_KEEP = 4


@dataclass(frozen=True, slots=True)
class SecretFinding:
    """Location of a discovered secret-like value."""

    path: str
    value: str


def scan_for_secrets(value: Any) -> list[SecretFinding]:
    """Recursively scan a nested value for secret-like tokens.

    Args:
        value: Arbitrary nested Python value.

    Returns:
        Secret findings with dotted/list paths.
    """
    findings: list[SecretFinding] = []
    _scan(value, "$", findings)
    return findings


def _scan(value: Any, path: str, findings: list[SecretFinding]) -> None:
    if isinstance(value, str):
        for pattern in _SECRET_RES:
            findings.extend(SecretFinding(path, _redact_secret(match.group(0))) for match in pattern.finditer(value))
    elif isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if isinstance(key, str) and _is_sensitive_key(key) and isinstance(child, str) and child:
                findings.append(SecretFinding(child_path, _redact_secret(child)))
            _scan(child, child_path, findings)
    elif isinstance(value, (list, tuple, set)):
        for index, child in enumerate(value):
            _scan(child, f"{path}[{index}]", findings)


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _redact_secret(value: str) -> str:
    """Return a stable non-secret preview for evidence and logs."""
    text = str(value)
    if len(text) <= _REDACTION_KEEP * 2:
        return "<redacted>"
    return f"{text[:_REDACTION_KEEP]}...{text[-_REDACTION_KEEP:]}<redacted>"
