"""Probe: reports CRITICAL when a tracked credential is near expiry."""

from __future__ import annotations

import datetime
import hashlib
import logging

from vetinari.workbench.spine_consumers import record_run_failed
from vetinari.workbench.status.contracts import ProbeResult

logger = logging.getLogger(__name__)

_CRITICAL_DAYS = 3
_DEGRADED_DAYS = 14


def credential_expiry_probe(
    credential_id: str,
    project_id: str,
    expiry: datetime.datetime,
    now: datetime.datetime | None = None,
) -> ProbeResult:
    """Check whether a credential is near expiry.

    Args:
        credential_id: Identifier for the credential being checked.
        project_id: Workbench project that owns this credential.
        expiry: UTC datetime when the credential expires.
        now: Optional current time override for tests.

    Returns:
        Critical, degraded, or ok depending on days remaining.
    """
    current = now or datetime.datetime.now(datetime.UTC)
    days_remaining = (expiry - current).total_seconds() / 86400
    if days_remaining <= _CRITICAL_DAYS:
        logger.warning(
            "status probe credential_expiry project_id=%s credential_id=%s critical days_remaining=%.1f",
            project_id,
            credential_id,
            days_remaining,
        )
        record_run_failed(
            run_id=f"credential-expiry-{project_id}-{_credential_handle(credential_id)}",
            kind="credential_expiry",
            project_id=project_id,
            error=f"credential expires in {days_remaining:.1f} days",
        )
        return ProbeResult(
            status="critical",
            message=f"Credential {_credential_handle(credential_id)} expires in {days_remaining:.1f} days",
            value=days_remaining,
        )
    if days_remaining <= _DEGRADED_DAYS:
        logger.warning(
            "status probe credential_expiry project_id=%s credential_id=%s degraded days_remaining=%.1f",
            project_id,
            credential_id,
            days_remaining,
        )
        return ProbeResult(
            status="degraded",
            message=f"Credential {_credential_handle(credential_id)} expires in {days_remaining:.1f} days",
            value=days_remaining,
        )
    return ProbeResult(
        status="ok",
        message=f"Credential {_credential_handle(credential_id)} valid for {days_remaining:.0f} days",
        value=days_remaining,
    )


def _credential_handle(credential_id: str) -> str:
    digest = hashlib.sha256(str(credential_id).encode("utf-8", errors="replace")).hexdigest()[:8]
    return f"<credential:{digest}>"
