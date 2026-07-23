"""Agent interface contracts — re-export hub for the three-agent factory pipeline.

Imports from sub-modules to stay under the 550-line file limit:
- interface_types.py: CapabilityType, Capability, AgentInterface data classes
- foreman_interface.py: FOREMAN_INTERFACE constant
- worker_interface.py: WORKER_INTERFACE constant
- inspector_interface_data.py: INSPECTOR_INTERFACE constant
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from vetinari.agents.foreman_interface import FOREMAN_INTERFACE
from vetinari.agents.inspector_interface_data import INSPECTOR_INTERFACE
from vetinari.agents.interface_types import AgentInterface, Capability, CapabilityType
from vetinari.agents.worker_interface import WORKER_INTERFACE
from vetinari.types import AgentType, WorkerMode

__all__ = [
    "AGENT_INTERFACES",
    "FOREMAN_INTERFACE",
    "INSPECTOR_INTERFACE",
    "WORKER_INTERFACE",
    "AgentInterface",
    "Capability",
    "CapabilityType",
    "CodingBridgeInterface",
    "DefaultInspectorInterface",
    "ForemanInterface",
    "InspectorInterface",
    "OperationDescriptor",
    "QualityInspectorInterface",
    "WorkerInterface",
    "get_agent_interface",
]


@dataclass(frozen=True, slots=True)
class OperationDescriptor:
    """Operation descriptor used by generated config-matrix tests."""

    name: str
    input_schema: dict[str, object]


class WorkerInterface:
    """Compatibility descriptor for Worker operations."""

    agent_type = AgentType.WORKER

    @staticmethod
    def supported_modes() -> list[WorkerMode]:
        """Return worker modes supported by this interface."""
        return list(WorkerMode)

    @staticmethod
    def operations() -> list[OperationDescriptor]:
        """Return worker operation descriptors."""
        return [
            OperationDescriptor("suggest", {}),
            OperationDescriptor("run_tests", {}),
            OperationDescriptor("edit_file", {}),
            OperationDescriptor("read_file", {}),
            OperationDescriptor("search_codebase", {}),
        ]


class CodingBridgeInterface:
    """Compatibility descriptor for coding bridge operations."""

    @staticmethod
    def operations() -> list[OperationDescriptor]:
        """Return coding operation descriptors."""
        return [
            OperationDescriptor("run_tests", {}),
            OperationDescriptor("edit_file", {}),
            OperationDescriptor("read_file", {}),
            OperationDescriptor("search_codebase", {}),
            OperationDescriptor("apply_diff", {}),
            OperationDescriptor("generate_code", {}),
            OperationDescriptor("explain_code", {}),
            OperationDescriptor("review_code", {}),
        ]


class ForemanInterface:
    """Compatibility descriptor for Foreman operations."""

    agent_type = AgentType.FOREMAN

    @staticmethod
    def operations() -> list[OperationDescriptor]:
        """Return Foreman operation descriptors."""
        return [OperationDescriptor("suggest", {})]


class InspectorInterface(ABC):
    """Compatibility descriptor for Inspector operations."""

    agent_type = AgentType.INSPECTOR

    @staticmethod
    @abstractmethod
    def operations() -> list[OperationDescriptor]:
        """Return inspector operation descriptors."""


class DefaultInspectorInterface(InspectorInterface):
    """Concrete compatibility descriptor for Inspector operations."""

    @staticmethod
    def operations() -> list[OperationDescriptor]:
        """Return inspector operation descriptors."""
        return [
            OperationDescriptor("review_code", {}),
            OperationDescriptor("verify_evidence", {}),
            OperationDescriptor("assess_quality", {}),
        ]


class QualityInspectorInterface(InspectorInterface):
    """Concrete compatibility descriptor for quality-focused Inspector operations."""

    @staticmethod
    def operations() -> list[OperationDescriptor]:
        """Return inspector quality operation descriptors."""
        return [
            OperationDescriptor("audit_findings", {}),
            OperationDescriptor("request_changes", {}),
            OperationDescriptor("approve_evidence", {}),
        ]


# Interface registry — 3-agent model only
AGENT_INTERFACES: dict[str, AgentInterface] = {
    AgentType.FOREMAN.value: FOREMAN_INTERFACE,
    AgentType.WORKER.value: WORKER_INTERFACE,
    AgentType.INSPECTOR.value: INSPECTOR_INTERFACE,
}


def get_agent_interface(agent_type: str) -> AgentInterface | None:
    """Get interface contract for an agent type.

    Args:
        agent_type: The agent type string (e.g. "FOREMAN", "WORKER", "INSPECTOR").

    Returns:
        AgentInterface for the given type, or None if not registered.
    """
    return AGENT_INTERFACES.get(agent_type)
