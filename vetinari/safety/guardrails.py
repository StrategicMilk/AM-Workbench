"""Compatibility facade for Vetinari safety guardrails."""

from __future__ import annotations

import logging
from typing import Any

from vetinari.safety.guardrails_checks import (
    _SENSITIVE_DATA_PATTERNS,
    _TOXIC_PATTERNS,
    _check_jailbreak,
    _check_prompt_security,
    _check_sensitive_data,
    _check_toxic,
    _check_vector_context,
)
from vetinari.safety.guardrails_manager import GuardrailsManager, _module_is_available
from vetinari.safety.guardrails_types import GuardrailResult, RailContext, Violation

logger = logging.getLogger(__name__)


def get_guardrails() -> GuardrailsManager:
    """Return the singleton GuardrailsManager instance."""
    return GuardrailsManager()


def reset_guardrails() -> None:
    """Destroy the singleton GuardrailsManager so the next call recreates it."""
    with GuardrailsManager._class_lock:
        GuardrailsManager._instance = None


def redact_pii(text: str) -> str:
    """Redact detected PII using the shared guardrails manager."""
    return get_guardrails().redact_pii(text)


def redact_pii_payload(payload: Any) -> Any:
    """Redact PII from every string value in a JSON-like payload.

    Args:
        payload: Scalar, mapping, list, or tuple that may contain strings.

    Returns:
        A copy of the payload with string leaves redacted.
    """
    if isinstance(payload, str):
        return redact_pii(payload)
    if isinstance(payload, dict):
        return {str(key): redact_pii_payload(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [redact_pii_payload(value) for value in payload]
    if isinstance(payload, tuple):
        return tuple(redact_pii_payload(value) for value in payload)
    return payload


def guard_inference_input(prompt: str) -> str:
    """Check a prompt against guardrails before sending to inference.

    Args:
        prompt: Raw prompt text to validate.

    Returns:
        The original prompt when all guardrails allow it.

    Raises:
        SecurityError: If policy blocks the prompt or checking fails closed.
    """
    from vetinari.exceptions import SecurityError

    try:
        gm = get_guardrails()
        result = gm.check_input(prompt)
        if not result.allowed:
            reasons = ", ".join(v.description for v in result.violations) if result.violations else "policy violation"
            raise SecurityError(f"Input blocked by guardrails: {reasons}")
    except SecurityError:
        raise
    except Exception as exc:
        logger.warning("Guardrails check_input failed - fail-closed: %s", exc)
        raise SecurityError("Input blocked by guardrails (fail-closed on internal error)") from exc
    return prompt


def guard_inference_output(response: str) -> str:
    """Check an inference response against guardrails before returning it.

    Args:
        response: Raw inference response text to validate.

    Returns:
        The original response when all guardrails allow it.

    Raises:
        SecurityError: If policy blocks the response or checking fails closed.
    """
    from vetinari.exceptions import SecurityError

    try:
        gm = get_guardrails()
        result = gm.check_output(response)
        if not result.allowed:
            reasons = ", ".join(v.description for v in result.violations) if result.violations else "policy violation"
            raise SecurityError(f"Output blocked by guardrails: {reasons}")
    except SecurityError:
        raise
    except Exception as exc:
        logger.warning("Guardrails check_output failed - fail-closed: %s", exc)
        raise SecurityError("Output blocked by guardrails (fail-closed on internal error)") from exc
    return response


__all__ = [
    "_SENSITIVE_DATA_PATTERNS",
    "_TOXIC_PATTERNS",
    "GuardrailResult",
    "GuardrailsManager",
    "RailContext",
    "Violation",
    "_check_jailbreak",
    "_check_prompt_security",
    "_check_sensitive_data",
    "_check_toxic",
    "_check_vector_context",
    "_module_is_available",
    "get_guardrails",
    "guard_inference_input",
    "guard_inference_output",
    "redact_pii",
    "redact_pii_payload",
    "reset_guardrails",
]
