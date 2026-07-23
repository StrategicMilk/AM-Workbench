"""Approval-gated capability installer."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from vetinari.agents.contracts import OutcomeSignal, Provenance
from vetinari.capabilities import (
    CapabilityApprovalRequired,
    CapabilityInstallApproval,
    CapabilityInstallError,
    CapabilityInstallState,
    CapabilityKind,
    CapabilityMetadata,
    CapabilityState,
    get_capability_registry,
)
from vetinari.receipts.record import WorkReceipt, WorkReceiptKind
from vetinari.receipts.store import WorkReceiptStore
from vetinari.types import AgentType, EvidenceBasis

logger = logging.getLogger(__name__)


_INSTALL_APPROVALS: dict[str, CapabilityInstallApproval] = {}
_INSTALL_APPROVALS_LOCK = threading.Lock()
_KIND_INSTALL_LOCKS: dict[CapabilityKind, threading.Lock] = {}
_KIND_INSTALL_LOCKS_LOCK = threading.Lock()

_DEFAULT_APPROVAL_TTL_SECONDS = 300
_DEFAULT_INSTALL_TIMEOUT_SECONDS = 1800
_INSTALL_RECEIPT_PROJECT_ID = "default"
_SENSITIVE_ENV_TOKENS = ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "API_KEY", "AUTH")
_REDACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(token|secret|password|credential|api[_-]?key|auth)=([^\\s&]+)"),
    re.compile(r"(?i)(https?://)([^\\s/@:]+):([^\\s/@]+)@"),
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_kind_lock(kind: CapabilityKind) -> threading.Lock:
    """Return the per-kind install lock, creating it lazily."""
    with _KIND_INSTALL_LOCKS_LOCK:
        lock = _KIND_INSTALL_LOCKS.get(kind)
        if lock is None:
            lock = threading.Lock()
            _KIND_INSTALL_LOCKS[kind] = lock
        return lock


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _approval_age_seconds(approved_at_utc: str) -> float:
    approved = datetime.fromisoformat(approved_at_utc.replace("Z", "+00:00"))
    if approved.tzinfo is None:
        approved = approved.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - approved).total_seconds()


def _install_environment() -> dict[str, str]:
    """Return a subprocess environment with credential-bearing variables removed."""
    return {
        key: value
        for key, value in os.environ.items()
        if not any(token in key.upper() for token in _SENSITIVE_ENV_TOKENS)
    }


def _redact_install_output(value: str) -> str:
    """Redact credential-shaped fragments before errors reach API responses or receipts."""
    redacted = value
    for pattern in _REDACT_PATTERNS:
        if pattern.pattern.startswith("(?i)(https?://)"):
            redacted = pattern.sub(r"\1<redacted>:<redacted>@", redacted)
        else:
            redacted = pattern.sub(r"\1=<redacted>", redacted)
    return redacted


def request_install_approval(*, kind: CapabilityKind, approver_session_id: str) -> CapabilityInstallApproval:
    """Issue a fresh single-use approval token for ``kind``.

    Returns:
        CapabilityInstallApproval value produced by request_install_approval().
    """
    approval = CapabilityInstallApproval(uuid.uuid4().hex, kind, _utc_now_iso(), approver_session_id)
    with _INSTALL_APPROVALS_LOCK:
        _INSTALL_APPROVALS[approval.request_id] = approval
    return approval


def is_approval_valid(
    approval: CapabilityInstallApproval, *, approval_ttl_seconds: int = _DEFAULT_APPROVAL_TTL_SECONDS
) -> bool:
    """Return whether ``approval`` exists, matches its kind, and has not expired.

    Returns:
        Boolean indicating whether is approval valid.
    """
    with _INSTALL_APPROVALS_LOCK:
        stored = _INSTALL_APPROVALS.get(approval.request_id)
    if stored is None or getattr(stored.kind, "value", stored.kind) != getattr(approval.kind, "value", approval.kind):
        return False
    try:
        return _approval_age_seconds(stored.approved_at_utc) <= approval_ttl_seconds
    except ValueError:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return False


def install_capability(
    *,
    kind: CapabilityKind,
    approval: CapabilityInstallApproval,
    approval_ttl_seconds: int = _DEFAULT_APPROVAL_TTL_SECONDS,
    install_timeout_seconds: int = _DEFAULT_INSTALL_TIMEOUT_SECONDS,
) -> CapabilityState:
    """Install one capability after explicit approval.

    Returns:
        CapabilityState value produced by install_capability().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    registry = get_capability_registry()
    metadata = registry.lookup(kind)
    command = (sys.executable, "-m", "pip", "install", "-e", f".[{metadata.pip_extra}]")
    if not is_approval_valid(approval, approval_ttl_seconds=approval_ttl_seconds):
        raise _approval_required(kind, metadata, command)
    kind_lock = _get_kind_lock(kind)
    if not kind_lock.acquire(blocking=False):
        raise CapabilityInstallError(
            f"install of capability {kind.value!r} is already in progress",
            kind=kind,
            pip_extra=metadata.pip_extra,
            command=command,
        )
    try:
        with _INSTALL_APPROVALS_LOCK:
            stored = _INSTALL_APPROVALS.pop(approval.request_id, None)
        if stored is None or getattr(stored.kind, "value", stored.kind) != getattr(kind, "value", kind):
            raise _approval_required(kind, metadata, command)
        registry.record_install_attempt(kind)
        receipt_store = WorkReceiptStore()
        try:
            run = subprocess.run
            result = run(
                list(command),
                capture_output=True,
                text=True,
                timeout=install_timeout_seconds,
                cwd=_repo_root(),
                env=_install_environment(),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            error = _redact_install_output(f"timeout after {install_timeout_seconds}s: {exc}")
            registry.record_install_failure(kind, reason=error)
            _emit_install_receipt(receipt_store, kind, metadata, passed=False, error=error)
            raise CapabilityInstallError(error, kind=kind, pip_extra=metadata.pip_extra, command=command) from exc
        if result.returncode != 0:
            error = _redact_install_output(
                (result.stderr or result.stdout or f"returncode={result.returncode}").strip()
            )
            registry.record_install_failure(kind, reason=error)
            _emit_install_receipt(receipt_store, kind, metadata, passed=False, error=error)
            raise CapabilityInstallError(
                f"install of capability {kind.value!r} failed: {error}",
                kind=kind,
                pip_extra=metadata.pip_extra,
                command=command,
                stdout=_redact_install_output(result.stdout),
                stderr=_redact_install_output(result.stderr),
                returncode=result.returncode,
            )
        registry.record_install_success(kind)
        _emit_install_receipt(receipt_store, kind, metadata, passed=True, error=None)
        state = registry.get_state(kind)
        if getattr(state.install_state, "value", state.install_state) != CapabilityInstallState.INSTALLED.value:
            raise CapabilityInstallError(f"install of capability {kind.value!r} did not reach INSTALLED", kind=kind)
        return state
    finally:
        kind_lock.release()


def _approval_required(
    kind: CapabilityKind, metadata: CapabilityMetadata, command: tuple[str, ...]
) -> CapabilityApprovalRequired:
    return CapabilityApprovalRequired(
        f"install of capability {kind.value!r} requires a fresh matching approval",
        kind=kind,
        install_command=command,
        target_environment=metadata.target_environment,
        extra_packages=metadata.extra_packages,
        disk_impact_mb=metadata.disk_impact_mb,
        network_impact_mb=metadata.network_impact_mb,
        risk_level=metadata.risk_level,
        degraded_fallback=metadata.degraded_fallback,
    )


def _emit_install_receipt(
    store: WorkReceiptStore, kind: CapabilityKind, metadata: CapabilityMetadata, *, passed: bool, error: str | None
) -> None:
    """Append one receipt for a capability install attempt."""
    receipt = WorkReceipt(
        project_id=_INSTALL_RECEIPT_PROJECT_ID,
        agent_id=f"capability-installer-{kind.value}-{uuid.uuid4().hex[:8]}",
        agent_type=AgentType.RELEASE,
        kind=WorkReceiptKind.RELEASE_STEP,
        outcome=OutcomeSignal(
            passed=passed,
            score=1.0 if passed else 0.0,
            basis=EvidenceBasis.TOOL_EVIDENCE,
            provenance=Provenance(
                source="vetinari.setup.capability_installer",
                timestamp_utc=_utc_now_iso(),
                tool_name="capability_installer",
            ),
        ),
        inputs_summary=f"install capability={kind.value} extra={metadata.pip_extra}",
        outputs_summary=(f"install_succeeded={passed}" + (f"; error={error[:120]}" if error else "")),
    )
    try:
        store.append(receipt)
    except RuntimeError as exc:
        logger.warning("Capability install receipt emission skipped after install outcome was recorded: %s", exc)


__all__ = ["install_capability", "is_approval_valid", "request_install_approval"]
