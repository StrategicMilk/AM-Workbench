"""@protected_mutation decorator â€” guard for lifecycle-fenced destructive operations.

Any function that permanently deletes, purges, clears, or resets data must be
wrapped with this decorator.  The decorator:

1. Requires the caller to pass ``intent: ConfirmedIntent``; missing or invalid
   intent raises ``UnconfirmedDestructiveAction`` (fail-closed, Rule 2).
2. If ``recycle=True`` (default), retires the target via ``RecycleStore`` BEFORE
   invoking the wrapped function so the entity is restorable within the grace
   window.  If the recycle step fails, ``RecycleFailedAbort`` is raised and the
   destructive operation is aborted â€” the target is left untouched.  Rollback
   is the user's responsibility.
3. Invokes the wrapped function.
4. Emits a ``WorkReceipt(kind=DESTRUCTIVE_OP)`` with ``basis=HUMAN_ATTESTED``
   and ``use_case="INTENT_CONFIRMATION"``, recording action, target, confirmed_by,
   and recycle_record_id for the audit trail.

Supports both sync and async callables.  On failure inside the wrapped function,
a receipt is still emitted with ``passed=False`` and the recycle record is kept
(rollback is still possible within the grace window).
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from vetinari.exceptions import RecycleFailedAbort
from vetinari.safety.recycle import RecycleStore
from vetinari.security.redaction import redact_text

logger = logging.getLogger(__name__)


_SOURCE = "vetinari.safety.protected_mutation"


def _safe_audit_text(value: object | None) -> str:
    """Return a redacted one-line string for logs and receipts."""
    if value is None:
        return ""
    return redact_text(str(value)).replace("\r", " ").replace("\n", " ")


# Module-level singleton â€” RecycleStore() resolves config from
# safety_defaults.yaml on every construction.  Cache it so guarded calls do
# not pay that cost per invocation while keeping the established
# double-checked locking pattern.
_recycle_store_singleton: RecycleStore | None = None
_recycle_store_lock = threading.Lock()


def _get_recycle_store() -> RecycleStore:
    """Return the cached process-wide RecycleStore, creating it on first call.

    Returns:
        A shared ``RecycleStore`` instance configured from
        ``config/safety_defaults.yaml``.
    """
    global _recycle_store_singleton
    if _recycle_store_singleton is None:
        with _recycle_store_lock:
            if _recycle_store_singleton is None:
                _recycle_store_singleton = RecycleStore()
    return _recycle_store_singleton


def get_recycle_store() -> RecycleStore:
    """Return the shared process-wide RecycleStore singleton.

    Public alias for ``_get_recycle_store()``.  Use this from code outside
    the ``protected_mutation`` module (e.g. route handlers and cleanup paths)
    so they participate in the test-isolation contract provided by
    ``_reset_recycle_store_singleton_for_tests()``.

    Returns:
        A shared ``RecycleStore`` instance configured from
        ``config/safety_defaults.yaml``.
    """
    return _get_recycle_store()


def _reset_recycle_store_singleton_for_tests() -> None:
    """Test-only: drop the cached RecycleStore so the next call re-constructs.

    Tests that monkeypatch ``RecycleStore`` after the cache has been
    populated should call this before exercising the decorator so the
    patched class wins.
    """
    global _recycle_store_singleton
    with _recycle_store_lock:
        _recycle_store_singleton = None


# Public types


class DestructiveAction(str, Enum):
    """Enumeration of destructive actions that require confirmed intent.

    Each value corresponds to a distinct lifecycle-critical operation.
    Use these as the ``action`` argument to ``@protected_mutation``.
    """

    DELETE_PROJECT = "delete_project"
    CLEAR_OUTPUTS = "clear_outputs"
    RESET_TRAINING = "reset_training"
    PURGE_ARCHIVE = "purge_archive"
    PURGE_RECYCLE = "purge_recycle"
    RESET_PROJECT = "reset_project"


@dataclass(frozen=True, slots=True)
class ConfirmedIntent:
    """Proof of human confirmation required by ``@protected_mutation``.

    Attributes:
        confirmed_by: Identity of the human who confirmed the action.
        reason: Human-readable justification for the destructive operation.
        confirmed_at_utc: ISO-8601 UTC timestamp when confirmation was given.
            Defaults to the current UTC time if not supplied.

    Raises:
        ValueError: If ``confirmed_by`` or ``reason`` are empty or whitespace.
    """

    confirmed_by: str
    reason: str
    confirmed_at_utc: str = ""

    def __post_init__(self) -> None:
        """Validate required fields.

        Raises:
            ValueError: If confirmed_by or reason is empty/whitespace.
        """
        if not self.confirmed_by or not self.confirmed_by.strip():
            raise ValueError("ConfirmedIntent.confirmed_by must be non-empty")
        if not self.reason or not self.reason.strip():
            raise ValueError("ConfirmedIntent.reason must be non-empty")
        # Set confirmed_at_utc to now if not provided (frozen dataclass workaround).
        if not self.confirmed_at_utc:
            object.__setattr__(self, "confirmed_at_utc", datetime.now(timezone.utc).isoformat())


class UnconfirmedDestructiveAction(Exception):
    """Raised when a protected_mutation call lacks a valid ConfirmedIntent.

    This is the fail-closed sentinel: if confirmation is missing or invalid
    the operation NEVER proceeds (Rule 2 â€” no default-pass on security checks).
    """


# Decorator


def protected_mutation(
    action: DestructiveAction,
    *,
    recycle: bool = True,
    recycle_target_param: str = "path",
    project_id_param: str = "project_id",
) -> Callable:
    """Decorator that guards a destructive function with a confirmed-intent check.

    Returns:
        Value produced for the caller.
    """

    def decorator(fn: Callable) -> Callable:
        """Apply the guard to the target function, returning the appropriate wrapper."""
        if asyncio.iscoroutinefunction(fn):
            return _build_async_protected_wrapper(fn, action, recycle, recycle_target_param, project_id_param)
        return _build_sync_protected_wrapper(fn, action, recycle, recycle_target_param, project_id_param)

    return decorator


# Internal helpers


def _prepare_protected_call(
    fn: Callable,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    action: DestructiveAction,
    recycle: bool,
    recycle_target_param: str,
    project_id_param: str,
) -> tuple[ConfirmedIntent, str | None, str | None, str]:
    """Validate intent and perform pre-call recycling for a protected mutation."""
    intent, err = _extract_intent(kwargs)
    if err:
        raise UnconfirmedDestructiveAction(err)
    recycle_record_id, target_path = _maybe_recycle(fn, args, kwargs, recycle, recycle_target_param, intent, action)
    project_id = _extract_project_id(fn, args, kwargs, project_id_param)
    return intent, recycle_record_id, target_path, project_id


def _emit_protected_call_receipt(
    action: DestructiveAction,
    intent: ConfirmedIntent,
    project_id: str,
    target_path: str | None,
    recycle_record_id: str | None,
    success: bool,
    error_msg: str,
) -> None:
    """Emit the protected mutation receipt for a completed wrapper call."""
    emit_destructive_op_receipt(
        action=action,
        intent=intent,
        project_id=project_id,
        target_path=target_path,
        recycle_record_id=recycle_record_id,
        success=success,
        error_msg=error_msg,
    )


def _build_async_protected_wrapper(
    fn: Callable,
    action: DestructiveAction,
    recycle: bool,
    recycle_target_param: str,
    project_id_param: str,
) -> Callable:
    """Build the async protected mutation wrapper."""

    @functools.wraps(fn)
    async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
        intent, recycle_record_id, target_path, project_id = _prepare_protected_call(
            fn, args, kwargs, action, recycle, recycle_target_param, project_id_param
        )
        success = False
        error_msg = ""
        try:
            result = await fn(*args, **kwargs)
            success = True
            return result
        except Exception as exc:
            error_msg = str(exc)
            raise
        finally:
            _emit_protected_call_receipt(action, intent, project_id, target_path, recycle_record_id, success, error_msg)

    async_wrapper.__dict__["_is_protected_mutation"] = True
    return async_wrapper


def _build_sync_protected_wrapper(
    fn: Callable,
    action: DestructiveAction,
    recycle: bool,
    recycle_target_param: str,
    project_id_param: str,
) -> Callable:
    """Build the sync protected mutation wrapper."""

    @functools.wraps(fn)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        intent, recycle_record_id, target_path, project_id = _prepare_protected_call(
            fn, args, kwargs, action, recycle, recycle_target_param, project_id_param
        )
        success = False
        error_msg = ""
        try:
            result = fn(*args, **kwargs)
            success = True
            return result
        except Exception as exc:
            error_msg = str(exc)
            raise
        finally:
            _emit_protected_call_receipt(action, intent, project_id, target_path, recycle_record_id, success, error_msg)

    sync_wrapper.__dict__["_is_protected_mutation"] = True
    return sync_wrapper


def _extract_intent(kwargs: dict[str, Any]) -> tuple[ConfirmedIntent | None, str]:
    """Pull and validate ConfirmedIntent from keyword arguments.

    Args:
        kwargs: The keyword arguments passed to the wrapped function.

    Returns:
        A tuple of (intent, error_message).  If ``error_message`` is non-empty,
        the caller must raise ``UnconfirmedDestructiveAction(error_message)``.
    """
    raw = kwargs.pop("intent", None)
    if raw is None:
        return None, (
            "Missing required 'intent: ConfirmedIntent' argument. "
            "Destructive operations require an explicit confirmed intent with "
            "confirmed_by and reason fields."
        )
    if not isinstance(raw, ConfirmedIntent):
        return None, (
            f"'intent' must be a ConfirmedIntent instance, got {type(raw).__name__}. "
            "Construct ConfirmedIntent(confirmed_by=..., reason=...) and pass it."
        )
    return raw, ""


def _maybe_recycle(
    fn: Callable,
    args: tuple,
    kwargs: dict[str, Any],
    recycle: bool,
    recycle_target_param: str,
    intent: ConfirmedIntent | None,
    action: DestructiveAction | None = None,
) -> tuple[str | None, str | None]:
    """Retire the target path via RecycleStore if recycle=True.

    If the recycle step fails, raises ``RecycleFailedAbort`` so the destructive
    operation is aborted with the target untouched (fail-closed, Rule 2).

    Args:
        fn: The wrapped function (used to bind argument names).
        args: Positional arguments to the wrapped function.
        kwargs: Keyword arguments to the wrapped function.
        recycle: Whether to recycle the target.
        recycle_target_param: Name of the parameter holding the target path.
        intent: The confirmed intent carrying the reason for recycling.
        action: The ``DestructiveAction`` being guarded (used in audit log).

    Returns:
        A tuple of (recycle_record_id, target_path_str).  Both are None if
        recycling was skipped or the parameter was not found.

    Raises:
        RecycleFailedAbort: If ``RecycleStore.retire`` raises; the destructive
            op must not proceed.
    """
    if not recycle or intent is None:
        return None, None

    try:
        sig = inspect.signature(fn)
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        raw_path = bound.arguments.get(recycle_target_param)
    except (TypeError, ValueError):
        raw_path = kwargs.get(recycle_target_param)

    if raw_path is None:
        return None, None

    target_path = Path(raw_path) if not isinstance(raw_path, Path) else raw_path

    if not target_path.exists():
        # Path already gone â€” nothing to recycle.
        return None, str(target_path)

    try:
        record = _get_recycle_store().retire(
            target_path,
            reason=_safe_audit_text(intent.reason),
            work_receipt_id=None,
        )
        logger.info(
            "protected_mutation: recycled %s -> record_id=%s (action=%s, reason=%s, confirmed_by=%s)",
            _safe_audit_text(target_path),
            record.record_id,
            action.value if action is not None else "unknown",
            _safe_audit_text(intent.reason),
            _safe_audit_text(intent.confirmed_by),
        )
        return record.record_id, str(target_path)
    except Exception as exc:
        safe_target = _safe_audit_text(target_path)
        safe_cause = _safe_audit_text(exc)
        logger.error(
            "protected_mutation: recycle of %s failed â€” %s â€” aborting destructive op (target untouched)",
            safe_target,
            safe_cause,
        )
        raise RecycleFailedAbort(
            f"Recycle of {safe_target} failed; destructive operation aborted. "
            f"Investigate disk/permission issues before retrying. Cause: {safe_cause}",
            target=safe_target,
        ) from exc


def _extract_project_id(
    fn: Callable,
    args: tuple,
    kwargs: dict[str, Any],
    project_id_param: str,
) -> str:
    """Extract the project_id from the call's bound arguments.

    Args:
        fn: The wrapped function.
        args: Positional arguments.
        kwargs: Keyword arguments.
        project_id_param: Name of the parameter holding the project ID.

    Returns:
        The project ID string, or ``"unknown"`` if not found.
    """
    try:
        sig = inspect.signature(fn)
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        pid = bound.arguments.get(project_id_param)
        if isinstance(pid, str) and pid.strip():
            return pid.strip()
    except (TypeError, ValueError):
        pid = kwargs.get(project_id_param)
        if isinstance(pid, str) and pid.strip():
            return pid.strip()
    return "unknown"


def emit_destructive_op_receipt(
    *,
    action: DestructiveAction,
    intent: ConfirmedIntent | None,
    project_id: str,
    target_path: str | None,
    recycle_record_id: str | None,
    success: bool,
    error_msg: str,
) -> None:
    """Emit a DESTRUCTIVE_OP WorkReceipt for audit purposes.

    Never raises â€” a failing receipt emission MUST NOT crash the caller.

    Args:
        action: The destructive action that was attempted.
        intent: The confirmed intent from the caller.
        project_id: Project owning this operation.
        target_path: String path of the target (for the receipt summary).
        recycle_record_id: Record ID of the recycle bin entry, or None.
        success: Whether the wrapped function completed without exception.
        error_msg: Error message if the wrapped function raised.
    """
    try:
        from vetinari.agents.contracts import OutcomeSignal, Provenance
        from vetinari.receipts.record import WorkReceipt, WorkReceiptKind
        from vetinari.receipts.store import WorkReceiptStore
        from vetinari.types import AgentType, EvidenceBasis

        confirmed_by = _safe_audit_text(intent.confirmed_by if intent else "unknown") or "unknown"
        safe_target = _safe_audit_text(target_path or "none") or "none"
        safe_error = _safe_audit_text(error_msg)

        inputs_summary = (f"action={action.value} | target={safe_target} | confirmed_by={confirmed_by}")[:200]

        outputs_parts = [f"success={success}"]
        if recycle_record_id:
            outputs_parts.append(f"recycle_record_id={recycle_record_id}")
        if safe_error:
            outputs_parts.append(f"error={safe_error[:80]}")
        outputs_summary = " | ".join(outputs_parts)[:200]

        outcome = OutcomeSignal(
            passed=success,
            score=1.0 if success else 0.0,
            basis=EvidenceBasis.HUMAN_ATTESTED,
            issues=(safe_error,) if safe_error else (),
            use_case="INTENT_CONFIRMATION",
            provenance=Provenance(
                source=_SOURCE,
                timestamp_utc=datetime.now(timezone.utc).isoformat(),
                attested_by=confirmed_by,
            ),
        )

        receipt = WorkReceipt(
            project_id=project_id,
            agent_id=f"protected_mutation:{action.value}",
            agent_type=AgentType.WORKER,
            kind=WorkReceiptKind.DESTRUCTIVE_OP,
            outcome=outcome,
            inputs_summary=inputs_summary,
            outputs_summary=outputs_summary,
        )
        WorkReceiptStore().append(receipt)
        logger.info(
            "protected_mutation: receipt emitted (action=%s, project=%s, passed=%s)",
            action.value,
            project_id,
            success,
        )
    except Exception as exc:
        logger.warning(
            "protected_mutation: failed to emit DESTRUCTIVE_OP receipt for action=%s â€” %s",
            action.value,
            exc,
        )


def requires_confirmed_intent(task: Any) -> bool:
    """Return True when a task carries the protected-mutation intent flag.

    Reads ``task.metadata.get("protected_mutation_intent_required", False)``
    so the flag written by ``mark_requires_tool_evidence`` has a canonical
    consumer (Rule 13 â€” no write-only data).

    Args:
        task: Any object with a ``metadata`` mapping attribute, typically a
            ``vetinari.agents.contracts.Task``.

    Returns:
        True if the task's metadata signals that a ``ConfirmedIntent`` is
        required before the destructive operation may proceed.
    """
    try:
        return task.metadata.get("protected_mutation_intent_required", False) is True
    except (AttributeError, TypeError) as exc:
        logger.debug("Task metadata unavailable while checking protected mutation intent: %s", exc)
        return False


def enforce_task_intent(task: Any, intent: ConfirmedIntent | None) -> None:
    """Raise if a protected task is dispatched without a confirmed intent.

    Call this at the Worker dispatch site before executing any task whose
    metadata carries ``protected_mutation_intent_required = True``.  This
    closes the write-only-data gap: ``mark_requires_tool_evidence`` writes
    the flag; this function enforces it.

    Args:
        task: Task-like object with a ``metadata`` mapping.
        intent: The caller-supplied ``ConfirmedIntent``, or ``None`` when
            no confirmation was provided.

    Raises:
        UnconfirmedDestructiveAction: If the task requires a confirmed intent
            but ``intent`` is ``None`` or not a ``ConfirmedIntent`` instance.
    """
    if not requires_confirmed_intent(task):
        return
    if intent is None or not isinstance(intent, ConfirmedIntent):
        task_id = getattr(task, "id", repr(task))
        raise UnconfirmedDestructiveAction(
            f"Task {task_id!r} carries protected_mutation_intent_required=True but "
            "no ConfirmedIntent was supplied. Pass intent=ConfirmedIntent(confirmed_by=..., "
            "reason=...) to the dispatch call before executing this destructive task."
        )


__all__ = [
    "ConfirmedIntent",
    "DestructiveAction",
    "UnconfirmedDestructiveAction",
    "emit_destructive_op_receipt",
    "enforce_task_intent",
    "get_recycle_store",
    "protected_mutation",
    "requires_confirmed_intent",
]
