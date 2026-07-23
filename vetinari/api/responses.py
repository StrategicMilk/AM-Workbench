"""Framework-neutral API response helpers for Vetinari.

All JSON API responses follow a consistent schema (ADR-0072):
- Success: ``{"status": "ok", "data": ..., "code": int, "api_version": __version__}``
- Error:   ``{"status": "error", "error": str, "code": int, "api_version": __version__}``
"""

from __future__ import annotations

import logging
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any, cast

import yaml

from vetinari import __version__
from vetinari.config_paths import resolve_config_path
from vetinari.errors import find_remediation

API_VERSION: str = __version__
_DEFAULT_SAFE_HTTP_DETAIL = "An unexpected error occurred. Check server logs for details."
logger = logging.getLogger(__name__)
_error_message_config: dict[str, Any] | None = None
_TECHNICAL_DETAIL_MARKERS = (
    "traceback",
    "runtimeerror",
    "valueerror",
    "permissionerror",
    "filenotfounderror",
    "connection refused",
    "cuda",
    "out of memory",
    "oom",
    "errno",
    " c:\\",
    "/home/",
    ".py",
)
_EXCEPTION_CODE_MAP: dict[str, str] = {
    "ValueError": "invalid_request",
    "KeyError": "invalid_request",
    "TypeError": "invalid_request",
    "FileNotFoundError": "not_found",
    "PermissionError": "forbidden",
    "TimeoutError": "timeout",
    "ConnectionError": "service_unavailable",
    "ExperimentStoreUnavailable": "service_unavailable",
    "MethodLibraryError": "invalid_request",
    "PlanRuntimeEditConflict": "conflict",
    "RagDebuggerError": "invalid_request",
    "RagEmbeddingModelMismatch": "conflict",
    "RagIndexMissing": "not_found",
    "RagQueryTooLarge": "payload_too_large",
    "ToolOutputSquasherError": "invalid_request",
    "WorkflowBuilderError": "invalid_request",
}


def success_response(data: Any = None, code: int = 200) -> dict[str, Any]:
    """Build a standard success response envelope."""
    return {"status": "ok", "data": data, "code": code, "api_version": API_VERSION}


def error_response(msg: str, code: int = 400, details: Any = None) -> dict[str, Any]:
    """Build a standard error response envelope.

    Args:
        msg: Client-safe error message.
        code: HTTP status code to include in the envelope.
        details: Optional structured details for the caller.

    Returns:
        JSON-compatible error response envelope.
    """
    body: dict[str, Any] = {"status": "error", "error": msg, "code": code, "api_version": API_VERSION}
    if details is not None:
        body["details"] = details
    remediation = _remediation_payload_for_error(msg)
    if remediation is not None:
        body["remediation"] = remediation
    return body


def json_safe(value: Any) -> Any:
    """Return a JSON-compatible representation of common API payload values.

    Returns:
        ``value`` converted to JSON-compatible primitives.
    """
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        dataclass_value = cast(Any, value)
        return {field.name: json_safe(getattr(dataclass_value, field.name)) for field in fields(dataclass_value)}
    if isinstance(value, tuple):
        return [json_safe(item) for item in value]
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    return value


def safe_error_code(exc: BaseException) -> str:
    """Return a stable opaque error code for API response bodies."""
    return _EXCEPTION_CODE_MAP.get(exc.__class__.__name__, "internal_error")


def safe_error_detail(detail: Any) -> str:
    """Return a client-safe detail string for recoverable API error bodies.

    Returns:
        Sanitized detail text that avoids leaking technical paths or tracebacks.
    """
    if isinstance(detail, BaseException):
        return safe_error_code(detail)
    raw = str(detail).strip() if detail is not None else ""
    if not raw:
        return "Bad request"
    friendly = _humanize_error_message(raw)
    if friendly != _DEFAULT_SAFE_HTTP_DETAIL:
        return str(friendly)
    raw_lower = raw.lower()
    if any(marker in raw_lower for marker in _TECHNICAL_DETAIL_MARKERS):
        return _DEFAULT_SAFE_HTTP_DETAIL
    return raw


def _load_error_message_config() -> dict[str, Any]:
    """Load user-safe error-message mappings without depending on web modules."""
    global _error_message_config
    if _error_message_config is not None:
        return _error_message_config
    config_path = resolve_config_path("error_messages.yaml")
    try:
        with open(config_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except FileNotFoundError:
        logger.warning("Error message config not found at %s; using built-in defaults", config_path)
        raw = {}
    except Exception:
        logger.exception("Failed to load error message config; using built-in defaults")
        raw = {}
    _error_message_config = raw if isinstance(raw, dict) else {}
    return _error_message_config


def _humanize_error_message(error_message: str) -> str:
    config = _load_error_message_config()
    msg_lower = error_message.lower()
    patterns = config.get("message_patterns", {})
    if isinstance(patterns, dict):
        for pattern, friendly_msg in patterns.items():
            if str(pattern).lower() in msg_lower:
                return str(friendly_msg)
    return str(config.get("default", _DEFAULT_SAFE_HTTP_DETAIL))


def _remediation_payload_for_error(msg: str) -> dict[str, Any] | None:
    remediation = find_remediation(msg)
    if remediation is None:
        return None
    return {
        "title": remediation.title,
        "explanation": remediation.explanation,
        "steps": list(remediation.steps),
        "severity": remediation.severity,
    }
