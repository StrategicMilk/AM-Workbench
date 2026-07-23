"""Loopback/session-bound IDE extension goal submission."""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SupportEnvelope:
    """Support payload returned when IDE submission validation fails."""

    code: str
    message: str
    recovery: str

    def to_dict(self) -> dict[str, str]:
        """Return a JSON-ready support payload."""
        return {"code": self.code, "message": self.message, "recovery": self.recovery}


@dataclass(frozen=True, slots=True)
class LocalFirstSubmission:
    """IDE goal submission bound to loopback, session, CSRF, and local actor."""

    goal: str
    session_token: str
    csrf_token: str
    origin: str
    remote_addr: str
    actor: str = "local_user"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"LocalFirstSubmission(origin={self.origin!r}, remote_addr={self.remote_addr!r}, actor={self.actor!r})"


class IdeSubmissionService:
    """Admit IDE goals only from a bound loopback Workbench session."""

    def __init__(self, *, bound_session_token: str, csrf_token: str, allowed_origins: tuple[str, ...]) -> None:
        self._bound_session_token = bound_session_token
        self._csrf_token = csrf_token
        self._allowed_origins = allowed_origins
        self.accepted_goals: list[str] = []
        self.receipts: list[dict[str, str]] = []

    def submit_goal(self, submission: LocalFirstSubmission) -> tuple[bool, dict[str, Any]]:
        """Validate and enqueue a local IDE goal submission.

        Returns:
            Acceptance flag and either a receipt payload or support envelope.
        """
        envelope = self._validate(submission)
        if envelope is not None:
            return False, {"support": envelope.to_dict(), "mutated": False}

        redacted_goal = _redact(submission.goal)
        self.accepted_goals.append(redacted_goal)
        receipt = {
            "receipt_id": f"ide:{len(self.receipts) + 1}",
            "caller": "extensions/ide",
            "write_path": "workbench.goal_queue",
            "permission": "tool:submit_goal",
            "status": "accepted",
        }
        self.receipts.append(receipt)
        return True, {"receipt": receipt, "goal": redacted_goal, "mutated": True}

    def _validate(self, submission: LocalFirstSubmission) -> SupportEnvelope | None:
        if not _is_loopback(submission.remote_addr):
            return SupportEnvelope(
                "IDE_REMOTE_DENIED", "remote IDE submission denied", "submit from loopback Workbench"
            )
        if submission.session_token != self._bound_session_token:
            return SupportEnvelope("IDE_SESSION_DENIED", "IDE session binding is invalid", "rebind the local session")
        if submission.origin not in self._allowed_origins:
            return SupportEnvelope(
                "IDE_ORIGIN_DENIED", "IDE origin is not trusted", "use the Workbench localhost origin"
            )
        if submission.csrf_token != self._csrf_token:
            return SupportEnvelope("IDE_CSRF_DENIED", "IDE CSRF token is invalid", "refresh the IDE session")
        if submission.actor != "local_user":
            return SupportEnvelope(
                "IDE_DEPUTY_DENIED", "IDE actor cannot submit for another principal", "submit as the bound local user"
            )
        if not submission.goal.strip():
            return SupportEnvelope("IDE_GOAL_EMPTY", "IDE goal is empty", "submit a non-empty goal")
        return None


def _is_loopback(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        logger.warning("Invalid IDE submission remote address rejected")
        return False


def _redact(value: str) -> str:
    lowered = value.lower()
    if any(marker in lowered for marker in ("secret", "token", "api_key", "password")):
        return "[redacted]"
    return value[:500]
