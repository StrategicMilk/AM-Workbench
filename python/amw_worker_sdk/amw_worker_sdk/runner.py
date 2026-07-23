"""Fail-closed execution wrapper for Python workers."""

from __future__ import annotations

import contextlib
import re
import socket
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from .manifest import WorkerIOField, WorkerManifest, expected_python_type

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PROMPT_CONTROL_RE = re.compile(
    r"(?is)(?:^|\b)(?:system|developer|assistant|tool)\s*[:=]|"
    r"\b(?:ignore|disregard|override|forget)\b.{0,80}\b(?:instructions?|rules?|guardrails?|policy)\b"
)


class WorkerOutputValidationError(RuntimeError):
    """Raised when worker output cannot be accepted."""

    def __init__(self, message: str, *, receipt: WorkerExecutionReceipt | None = None) -> None:
        super().__init__(message)
        self.receipt = receipt


@dataclass(frozen=True, slots=True)
class WorkerExecutionReceipt:
    """Receipt emitted after a supervised Python worker attempt."""

    worker_id: str
    status: str
    started_at_utc: str
    finished_at_utc: str
    recovery: str
    output_keys: tuple[str, ...]
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible receipt payload."""
        return asdict(self)


class WorkerRunner:
    """Execute callables through a worker manifest boundary."""

    def run_callable(
        self,
        manifest: WorkerManifest,
        inputs: dict[str, Any],
        worker: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> tuple[dict[str, Any], WorkerExecutionReceipt]:
        """Validate inputs, run the callable, validate outputs, and receipt."""
        started_at = _utc_now()
        try:
            _validate_payload("input", manifest.inputs, inputs)
            with _network_boundary(manifest.allow_network):
                outputs = worker(dict(inputs))
            if not isinstance(outputs, dict):
                raise WorkerOutputValidationError("worker output must be a dict")
            _validate_payload("output", manifest.outputs, outputs)
        except Exception as exc:
            finished_at = _utc_now()
            receipt = WorkerExecutionReceipt(
                worker_id=manifest.worker_id,
                status="failed",
                started_at_utc=started_at,
                finished_at_utc=finished_at,
                recovery=manifest.recovery.value,
                output_keys=(),
                error=_redact(str(exc)),
            )
            raise WorkerOutputValidationError(receipt.error, receipt=receipt) from exc
        finished_at = _utc_now()
        return outputs, WorkerExecutionReceipt(
            worker_id=manifest.worker_id,
            status="completed",
            started_at_utc=started_at,
            finished_at_utc=finished_at,
            recovery=manifest.recovery.value,
            output_keys=tuple(sorted(outputs)),
        )


def _validate_payload(label: str, fields: tuple[WorkerIOField, ...], payload: dict[str, Any]) -> None:
    expected = {field.name: field for field in fields}
    unexpected = sorted(set(payload) - set(expected))
    if unexpected:
        raise WorkerOutputValidationError(f"unexpected {label} field(s): {', '.join(unexpected)}")
    for field in fields:
        _validate_text_boundary("field name", field.name, max_length=160)
        if field.name not in payload:
            if field.required:
                raise WorkerOutputValidationError(f"missing required {label} field {field.name!r}")
            continue
        expected_type = expected_python_type(field.type_name)
        value = payload[field.name]
        if isinstance(value, str):
            _validate_text_boundary(f"{label} field {field.name!r}", value, max_length=20_000)
        if field.type_name == "bool":
            valid = isinstance(value, bool)
        elif field.type_name in {"int", "float"}:
            valid = isinstance(value, expected_type) and not isinstance(value, bool)
        else:
            valid = isinstance(value, expected_type)
        if not valid:
            raise WorkerOutputValidationError(
                f"{label} field {field.name!r} expected {field.type_name}, got {type(value).__name__}"
            )


def _validate_text_boundary(label: str, value: str, *, max_length: int) -> str:
    text = str(value)
    if len(text) > max_length:
        raise WorkerOutputValidationError(f"{label} exceeds maximum length")
    if _CONTROL_CHARS_RE.search(text):
        raise WorkerOutputValidationError(f"{label} contains control characters")
    if _PROMPT_CONTROL_RE.search(text):
        raise WorkerOutputValidationError(f"{label} contains prompt-control markers")
    return text


def _redact(message: str) -> str:
    try:
        from vetinari.security.redaction import redact_text
    except Exception:
        return _fallback_redact_text(message)
    return redact_text(message)


def _fallback_redact_text(message: str) -> str:
    redacted = str(message)
    redacted = re.sub(
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|secret|token)\s*[:=]\s*[^\s,;]+",
        lambda match: f"{match.group(1)}=<redacted>",
        redacted,
    )
    redacted = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+", "bearer <redacted>", redacted)
    return redacted


@contextlib.contextmanager
def _network_boundary(allow_network: bool):
    if allow_network:
        yield
        return

    original_socket = socket.socket
    original_create_connection = socket.create_connection

    def _deny_network(*_args: Any, **_kwargs: Any) -> socket.socket:
        raise WorkerOutputValidationError("worker network access requires allow_network=True")

    socket.socket = _deny_network  # type: ignore[assignment]
    socket.create_connection = _deny_network  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket = original_socket  # type: ignore[assignment]
        socket.create_connection = original_create_connection


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
