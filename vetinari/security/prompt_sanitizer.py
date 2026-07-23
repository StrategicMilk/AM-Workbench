"""Prompt-role sanitization helpers."""

# TODO(#5516): FSA-5516 real wiring in vetinari/agents/consolidated/worker_agent.py Task 1.5

from __future__ import annotations

import re
from typing import Any

from vetinari.privacy.envelope import require_privacy_envelope

_LEADING_ROLE_RE = re.compile(r"^\s*(?:<\|(?:system|assistant|user)\|>|(?:system|assistant|user)\s*:)", re.IGNORECASE)


def sanitize_prompt(prompt: str) -> str:
    """Remove leading role-control markers from a prompt fragment.

    Args:
        prompt: Untrusted prompt fragment.

    Returns:
        Prompt text with leading role markers stripped.
    """
    return _LEADING_ROLE_RE.sub("", prompt).lstrip()


def sanitize_prompt_record(record: dict[str, Any]) -> dict[str, Any]:
    """Sanitize an envelope-wrapped prompt record or fail closed.

    Args:
        record: Privacy-envelope-wrapped prompt record with a text payload or a
            dictionary payload containing a text ``prompt`` field.

    Returns:
        A shallow copy of ``record`` with role-control markers removed from the
        prompt payload.

    Raises:
        ValueError: Raised when the payload is neither text nor a dictionary
            with a text ``prompt`` field.
        PrivacyEnvelopeError: Raised by ``require_privacy_envelope`` when the
            record is missing the required privacy envelope.
    """
    require_privacy_envelope(record)
    payload = record.get("payload")
    if isinstance(payload, str):
        sanitized_payload: Any = sanitize_prompt(payload)
    elif isinstance(payload, dict) and isinstance(payload.get("prompt"), str):
        sanitized_payload = dict(payload)
        sanitized_payload["prompt"] = sanitize_prompt(payload["prompt"])
    else:
        raise ValueError("prompt record payload must be text or contain a text prompt field")
    sanitized = dict(record)
    sanitized["payload"] = sanitized_payload
    return sanitized


__all__ = ["sanitize_prompt", "sanitize_prompt_record"]
