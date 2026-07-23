"""Privacy redaction for network transport evidence."""

from __future__ import annotations

import re
from typing import Any

from vetinari.workbench.network.contracts import NetworkTransportError

_IPV4 = re.compile(r"\b(?:10|127|169\.254|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.(?:\d{1,3}\.){1,2}\d{1,3}\b")
_HOST = re.compile(r"\b(?:[a-zA-Z0-9-]+\.)+(?:local|lan|internal|home|corp)\b")
_PATH = re.compile(r"[A-Za-z]:\\[^,\n\r;]+|/(?:Users|home|mnt|dev)/[^,\n\r;]+")
_SECRET_KEY_PARTS = (
    "secret",
    "token",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "key",
    "credential",
    "authorization",
    "access_token",
    "client_secret",
)
_QUERY = re.compile(
    r"([?&](?:q|query|prompt|token|key|password|access_token|api_key|apikey|client_secret)=)(?!\[redacted\])[^&\s]+",
    re.IGNORECASE,
)
_HEADER = re.compile(
    r"\b(authorization|cookie|x-api-key|api-key|provider-account-id|request-id):\s+(?!\[redacted\])[^\n\r]+",
    re.IGNORECASE,
)
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)


def redact_network_evidence(payload: Any, *, allowlist: tuple[str, ...] = ()) -> Any:
    """Return a copy of payload with private network and credential strings removed.

    Returns:
        Any value produced by redact_network_evidence().
    """
    if isinstance(payload, dict):
        redacted: dict[str, Any] = {}
        for key, value in payload.items():
            key_text = str(key)
            if _is_secret_key(key_text) and isinstance(value, str) and value not in allowlist:
                redacted[key_text] = "[redacted]"
            else:
                redacted[key_text] = redact_network_evidence(value, allowlist=allowlist)
        return redacted
    if isinstance(payload, list):
        return [redact_network_evidence(item, allowlist=allowlist) for item in payload]
    if isinstance(payload, tuple):
        return tuple(redact_network_evidence(item, allowlist=allowlist) for item in payload)
    if isinstance(payload, str):
        if payload in allowlist:
            return payload
        return _redact_text(payload, allowlist)
    return payload


def assert_redacted(payload: Any) -> None:
    """Fail closed when private evidence remains in a payload.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if isinstance(payload, dict):
        for key, value in payload.items():
            if _is_secret_key(str(key)) and isinstance(value, str) and value != "[redacted]":
                raise NetworkTransportError("network-evidence-not-redacted")
            assert_redacted(value)
        return
    if isinstance(payload, (list, tuple)):
        for value in payload:
            assert_redacted(value)
        return
    if not isinstance(payload, str):
        return
    if (
        _IPV4.search(payload)
        or _HOST.search(payload)
        or _HEADER.search(payload)
        or _PATH.search(payload)
        or _QUERY.search(payload)
        or _BEARER.search(payload)
    ):
        raise NetworkTransportError("network-evidence-not-redacted")


def _redact_text(value: str, allowlist: tuple[str, ...]) -> str:
    redacted = value
    for allowed in allowlist:
        redacted = redacted.replace(allowed, f"__ALLOWLIST_{allowlist.index(allowed)}__")
    redacted = _HEADER.sub(lambda match: f"{match.group(1)}: [redacted]", redacted)
    redacted = _QUERY.sub(lambda match: f"{match.group(1)}[redacted]", redacted)
    redacted = _BEARER.sub("Bearer [redacted]", redacted)
    redacted = _IPV4.sub("[private-ip]", redacted)
    redacted = _HOST.sub("[private-host]", redacted)
    redacted = _PATH.sub("[local-path]", redacted)
    for idx, allowed in enumerate(allowlist):
        redacted = redacted.replace(f"__ALLOWLIST_{idx}__", allowed)
    return redacted


def _is_secret_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_").replace(".", "_")
    return any(part in normalized for part in _SECRET_KEY_PARTS)


__all__ = ["assert_redacted", "redact_network_evidence"]
