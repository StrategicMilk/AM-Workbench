"""Central redaction helpers for route responses, exports, and logs."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

from vetinari.security import get_secret_scanner

REDACTED = "[REDACTED]"
REDACTED_PATH = "[REDACTED_PATH]"
REDACTED_URL = "[REDACTED_URL]"
REDACTED_EMAIL = "[REDACTED_EMAIL]"

_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])(?:[A-Za-z]:\\(?:[^\s\\/\r\n\t:*?\"<>|]+\\?)+)")
_UNIX_ABSOLUTE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])/(?:Users|home|var|tmp|etc|opt|mnt|srv|root|dev)/[^\s'\"<>)]*")
_SENSITIVE_URL_RE = re.compile(
    r"https?://[^\s'\"<>)]*(?:token|key|secret|password|credential|authorization|webhook)[^\s'\"<>)]*",
    re.IGNORECASE,
)
_PROVIDER_URL_RE = re.compile(
    r"https?://(?:api\.openai\.com|api\.anthropic\.com|generativelanguage\.googleapis\.com|api\.replicate\.com|huggingface\.co|api\.huggingface\.co)[^\s'\"<>)]*",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_LABELLED_SECRET_RE = re.compile(
    r"(?P<label>\b(?:api[\s_.-]?key|api[\s_.-]?token|access[\s_.-]?token|refresh[\s_.-]?token|bearer|authorization|password|secret|credential|webhook[\s_.-]?url|provider[\s_.-]?url|token)\b\s*(?::|=|\s)\s*)(?P<value>[^\s,;]{4,})",
    re.IGNORECASE,
)
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

_SENSITIVE_KEY_TOKENS = frozenset({
    "api_key",
    "apikey",
    "api_secret",
    "access_key",
    "access_token",
    "authorization",
    "bearer",
    "client_secret",
    "credential",
    "credentials",
    "goal",
    "local_path",
    "log_detail",
    "log_details",
    "password",
    "private_key",
    "prompt",
    "provider_url",
    "query",
    "refresh_token",
    "secret",
    "token",
    "tool_args",
    "tool_output",
    "tool_result",
    "trace_detail",
    "trace_details",
    "trace_log_detail",
    "trace_log_details",
    "transcript_text",
    "webhook_url",
})

_PII_REPR_KEYS = frozenset({
    "candidate",
    "context",
    "device",
    "prompt",
    "query",
    "subject",
    "transcript_text",
})


def _normalise_key(key: str) -> str:
    spaced = _CAMEL_BOUNDARY_RE.sub("_", str(key))
    return re.sub(r"[\s.\-]+", "_", spaced).lower()


def is_sensitive_key(key: object) -> bool:
    """Return whether a mapping key should have its value fully redacted.

    Returns:
        True when the key name is treated as sensitive.
    """
    normalised = _normalise_key(str(key))
    if normalised in _SENSITIVE_KEY_TOKENS:
        return True
    return any(
        normalised.endswith(suffix)
        for suffix in (
            "_api_key",
            "_apikey",
            "_credential",
            "_credentials",
            "_key",
            "_password",
            "_prompt",
            "_secret",
            "_token",
            "_url",
        )
    )


def redact_text(value: str) -> str:
    """Redact credentials, provider URLs, labelled secrets, email addresses, and host paths.

    Returns:
        Text with sensitive values replaced by stable redaction tokens.
    """
    if not isinstance(value, str):
        value = str(value)

    redacted = _SENSITIVE_URL_RE.sub(REDACTED_URL, value)
    redacted = _PROVIDER_URL_RE.sub(REDACTED_URL, redacted)
    redacted = _WINDOWS_ABSOLUTE_PATH_RE.sub(REDACTED_PATH, redacted)
    redacted = _UNIX_ABSOLUTE_PATH_RE.sub(REDACTED_PATH, redacted)
    scanner = get_secret_scanner()
    redacted = scanner.redact(redacted)
    redacted = _EMAIL_RE.sub(REDACTED_EMAIL, redacted)
    return _LABELLED_SECRET_RE.sub(lambda m: f"{m.group('label')}{REDACTED}", redacted)


def redact_value(value: Any) -> Any:
    """Recursively redact strings and sensitive-key mapping values.

    Returns:
        Redacted value preserving the input container shape where practical.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        result: dict[Any, Any] = {}
        for key, item in value.items():
            result[key] = REDACTED if is_sensitive_key(key) else redact_value(item)
        return result
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [redact_value(item) for item in value]
    return value


def redact_route_payload(payload: Any) -> Any:
    """Redact data before returning it from unauthenticated or export surfaces."""
    return redact_value(payload)


def redact_repr(cls_name: str, fields: Mapping[str, Any]) -> str:
    """Build a compact repr with sensitive and PII fields summarized.

    Args:
        cls_name: Class name to display in the repr.
        fields: Field names and values to render.

    Returns:
        Safe repr string.
    """
    rendered: list[str] = []
    for key, value in fields.items():
        if is_sensitive_key(key) or _normalise_key(str(key)) in _PII_REPR_KEYS:
            rendered.append(f"{key}=<{_summary(value)}>")
        else:
            rendered.append(f"{key}={redact_value(value)!r}")
    return f"{cls_name}({', '.join(rendered)})"


def _summary(value: Any) -> str:
    text = str(value)
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:8]
    return f"redacted:{len(text)} chars:{digest}"
