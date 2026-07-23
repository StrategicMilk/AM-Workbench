"""Routing table helpers for :mod:`vetinari.a2a.executor`."""

from __future__ import annotations

import logging

from vetinari.a2a.executor_models import _RouteEntry, _RoutingTable
from vetinari.types import AgentType

logger = logging.getLogger(__name__)


class A2ARoutingMixin:
    """Provide task type lookup behavior for the A2A executor facade."""

    _routing_table: _RoutingTable

    def _route_to_agent(self, task_type: str) -> _RouteEntry | None:
        """Look up the (AgentType, mode) pair for a given task type string.

        Args:
            task_type: The A2A task type string to look up.

        Returns:
            A ``(AgentType, mode)`` tuple if the task type is recognised,
            or ``None`` if it is not in the routing table.
        """
        return self._routing_table.get(task_type)

    @staticmethod
    def _build_routing_table() -> _RoutingTable:
        """Construct the full A2A task-type to agent/mode routing table.

        Returns:
            Mapping from task type string to ``(AgentType, mode)`` tuples.
        """
        table: _RoutingTable = {
            "plan": (AgentType.FOREMAN, "plan"),
            "clarify": (AgentType.FOREMAN, "clarify"),
            "consolidate": (AgentType.FOREMAN, "consolidate"),
            "summarise": (AgentType.FOREMAN, "summarise"),
            "summarize": (AgentType.FOREMAN, "summarise"),
            "prune": (AgentType.FOREMAN, "prune"),
            "extract": (AgentType.FOREMAN, "extract"),
            "research": (AgentType.WORKER, "code_discovery"),
            "code_discovery": (AgentType.WORKER, "code_discovery"),
            "domain_research": (AgentType.WORKER, "domain_research"),
            "api_lookup": (AgentType.WORKER, "api_lookup"),
            "lateral_thinking": (AgentType.WORKER, "lateral_thinking"),
            "ui_design": (AgentType.WORKER, "ui_design"),
            "database": (AgentType.WORKER, "database"),
            "devops": (AgentType.WORKER, "devops"),
            "git_workflow": (AgentType.WORKER, "git_workflow"),
            "architecture": (AgentType.WORKER, "architecture"),
            "risk_assessment": (AgentType.WORKER, "risk_assessment"),
            "ontological_analysis": (AgentType.WORKER, "ontological_analysis"),
            "contrarian_review": (AgentType.WORKER, "contrarian_review"),
            "suggest": (AgentType.WORKER, "suggest"),
            "build": (AgentType.WORKER, "build"),
            "implement": (AgentType.WORKER, "build"),
            "image_generation": (AgentType.WORKER, "image_generation"),
            "documentation": (AgentType.WORKER, "documentation"),
            "creative_writing": (AgentType.WORKER, "creative_writing"),
            "cost_analysis": (AgentType.WORKER, "cost_analysis"),
            "experiment": (AgentType.WORKER, "experiment"),
            "error_recovery": (AgentType.WORKER, "error_recovery"),
            "synthesis": (AgentType.WORKER, "synthesis"),
            "improvement": (AgentType.WORKER, "improvement"),
            "monitor": (AgentType.WORKER, "monitor"),
            "devops_ops": (AgentType.WORKER, "devops_ops"),
            "review": (AgentType.INSPECTOR, "code_review"),
            "code_review": (AgentType.INSPECTOR, "code_review"),
            "security_audit": (AgentType.INSPECTOR, "security_audit"),
            "test_generation": (AgentType.INSPECTOR, "test_generation"),
            "simplification": (AgentType.INSPECTOR, "simplification"),
        }
        logger.debug("A2A routing table built with %d entries", len(table))
        return table
