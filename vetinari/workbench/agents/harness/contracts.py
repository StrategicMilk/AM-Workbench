"""Deterministic agent-run harness contracts for AM Workbench."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Any

logger = logging.getLogger(__name__)


BLOCKER_TOOL_NOT_ALLOWED = "tool_not_allowed"
BLOCKER_WORKSPACE_ESCAPE = "workspace_escape"
BLOCKER_RECEIPT_MISSING = "receipt_missing"


class AgentRunHarnessError(ValueError):
    """Raised when an agent run cannot be admitted safely."""


class NetworkExposure(str, Enum):
    """Network exposure granted to a run."""

    DENIED = "denied"
    ALLOWLISTED = "allowlisted"


class ProcessExposure(str, Enum):
    """Process exposure granted to a run."""

    NONE = "none"
    LIMITED = "limited"


@dataclass(frozen=True, slots=True)
class SandboxProfile:
    """Deterministic sandbox, permissions, and replay contract."""

    sandbox_id: str
    workspace_ref: str
    allowed_workspace_prefix: str
    tool_permissions: tuple[str, ...]
    file_exposure: tuple[str, ...]
    network_exposure: NetworkExposure
    process_exposure: ProcessExposure
    model_policy_ref: str
    memory_profile_ref: str
    input_schema_ref: str
    output_schema_ref: str
    receipt_requirements: tuple[str, ...]
    cancellation_behavior: str
    replay_boundary_ref: str
    authority_ref: str
    provenance_ref: str

    def __post_init__(self) -> None:
        for field_name in (
            "sandbox_id",
            "workspace_ref",
            "allowed_workspace_prefix",
            "model_policy_ref",
            "memory_profile_ref",
            "input_schema_ref",
            "output_schema_ref",
            "cancellation_behavior",
            "replay_boundary_ref",
            "authority_ref",
            "provenance_ref",
        ):
            _require_text(getattr(self, field_name), field_name)
        _require_string_tuple(self.tool_permissions, "tool_permissions")
        _require_string_tuple(self.file_exposure, "file_exposure")
        _require_string_tuple(self.receipt_requirements, "receipt_requirements")
        if not isinstance(self.network_exposure, NetworkExposure):
            raise AgentRunHarnessError("network_exposure must be NetworkExposure")
        if not isinstance(self.process_exposure, ProcessExposure):
            raise AgentRunHarnessError("process_exposure must be ProcessExposure")

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = asdict(self)
        payload["network_exposure"] = self.network_exposure.value
        payload["process_exposure"] = self.process_exposure.value
        payload["tool_permissions"] = list(self.tool_permissions)
        payload["file_exposure"] = list(self.file_exposure)
        payload["receipt_requirements"] = list(self.receipt_requirements)
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SandboxProfile(sandbox_id={self.sandbox_id!r}, workspace_ref={self.workspace_ref!r}, allowed_workspace_prefix={self.allowed_workspace_prefix!r})"


@dataclass(frozen=True, slots=True)
class AgentRunRequest:
    """Agent run declaration before any model work starts."""

    run_id: str
    template_id: str
    sandbox: SandboxProfile
    requested_tools: tuple[str, ...]
    workspace_path: str
    input_payload_ref: str
    expected_output_ref: str
    receipt_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in ("run_id", "template_id", "workspace_path", "input_payload_ref", "expected_output_ref"):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.sandbox, SandboxProfile):
            raise AgentRunHarnessError("sandbox must be SandboxProfile")
        _require_string_tuple(self.requested_tools, "requested_tools")
        _require_string_tuple(self.receipt_refs, "receipt_refs", allow_empty=True)

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = asdict(self)
        payload["sandbox"] = self.sandbox.to_dict()
        payload["requested_tools"] = list(self.requested_tools)
        payload["receipt_refs"] = list(self.receipt_refs)
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AgentRunRequest(run_id={self.run_id!r}, template_id={self.template_id!r}, sandbox={self.sandbox!r})"


@dataclass(frozen=True, slots=True)
class AgentRunAdmission:
    """Deterministic admission result for an agent run."""

    run_id: str
    admitted: bool
    blockers: tuple[str, ...]
    admitted_tools: tuple[str, ...]
    replay_boundary_ref: str
    cancellation_behavior: str

    def __post_init__(self) -> None:
        _require_text(self.run_id, "run_id")
        _require_string_tuple(self.blockers, "blockers", allow_empty=True)
        _require_string_tuple(self.admitted_tools, "admitted_tools", allow_empty=True)
        _require_text(self.replay_boundary_ref, "replay_boundary_ref")
        _require_text(self.cancellation_behavior, "cancellation_behavior")
        if self.admitted and self.blockers:
            raise AgentRunHarnessError("admitted run cannot include blockers")
        if not self.admitted and not self.blockers:
            raise AgentRunHarnessError("blocked run requires blockers")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AgentRunAdmission(run_id={self.run_id!r}, admitted={self.admitted!r}, blockers={self.blockers!r})"


def admit_agent_run(request: AgentRunRequest) -> AgentRunAdmission:
    """Run deterministic tool, workspace, and receipt checks before model execution.

    Returns:
        AgentRunAdmission value produced by admit_agent_run().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not isinstance(request, AgentRunRequest):
        raise AgentRunHarnessError("request must be AgentRunRequest")
    blockers: list[str] = []
    allowed_tools = set(request.sandbox.tool_permissions)
    if any(tool not in allowed_tools for tool in request.requested_tools):
        blockers.append(BLOCKER_TOOL_NOT_ALLOWED)
    if not _path_is_inside(request.workspace_path, request.sandbox.allowed_workspace_prefix):
        blockers.append(BLOCKER_WORKSPACE_ESCAPE)
    missing_receipts = set(request.sandbox.receipt_requirements) - set(request.receipt_refs)
    if missing_receipts:
        blockers.append(BLOCKER_RECEIPT_MISSING)
    return AgentRunAdmission(
        run_id=request.run_id,
        admitted=not blockers,
        blockers=tuple(dict.fromkeys(blockers)),
        admitted_tools=tuple(tool for tool in request.requested_tools if tool in allowed_tools),
        replay_boundary_ref=request.sandbox.replay_boundary_ref,
        cancellation_behavior=request.sandbox.cancellation_behavior,
    )


def _path_is_inside(path: str, prefix: str) -> bool:
    candidate = PurePosixPath(path.replace("\\", "/"))
    root = PurePosixPath(prefix.replace("\\", "/"))
    if ".." in candidate.parts:
        return False
    try:
        candidate.relative_to(root)
    except ValueError:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return False
    return True


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise AgentRunHarnessError(f"{field_name} must be non-empty")


def _require_string_tuple(values: tuple[str, ...], field_name: str, *, allow_empty: bool = False) -> None:
    if not isinstance(values, tuple) or (not allow_empty and not values):
        raise AgentRunHarnessError(f"{field_name} must be a non-empty tuple")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise AgentRunHarnessError(f"{field_name} must contain non-empty strings")


__all__ = [
    "BLOCKER_RECEIPT_MISSING",
    "BLOCKER_TOOL_NOT_ALLOWED",
    "BLOCKER_WORKSPACE_ESCAPE",
    "AgentRunAdmission",
    "AgentRunHarnessError",
    "AgentRunRequest",
    "NetworkExposure",
    "ProcessExposure",
    "SandboxProfile",
    "admit_agent_run",
]
