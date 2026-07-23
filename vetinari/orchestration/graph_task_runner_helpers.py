"""Lazy dependency accessors for graph task execution."""

from __future__ import annotations

import importlib.util
import logging
import sys
from collections.abc import Callable
from functools import cache
from typing import Any

logger = logging.getLogger(__name__)


def _module_is_available(module_name: str) -> bool:
    """Return True when an optional graph-runner dependency is discoverable."""
    if module_name in sys.modules:
        return sys.modules[module_name] is not None
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ModuleNotFoundError, ValueError):
        logger.debug("Optional graph-runner dependency is unavailable: %s", module_name)
        return False


@cache
def _get_agent_control_state_fn() -> Callable[[], dict[str, Any]]:
    """Load the operator pause-state accessor once per process."""
    from vetinari.orchestration.agent_control import get_agent_control_state

    return get_agent_control_state


@cache
def _get_log_event_fn() -> Callable[..., None]:
    """Load the structured logging event emitter once per process."""
    from vetinari.structured_logging import log_event

    return log_event


@cache
def _get_andon_system_fn() -> Callable[[], Any]:
    """Load the Andon system accessor once per process."""
    from vetinari.workflow.andon import get_andon_system

    return get_andon_system


@cache
def _get_bottleneck_identifier_fn() -> Callable[[], Any]:
    """Load the bottleneck tracker accessor once per process."""
    from vetinari.orchestration.bottleneck import get_bottleneck_identifier

    return get_bottleneck_identifier


@cache
def _get_constraint_registry_fn() -> Callable[[], Any]:
    """Load the constraint registry accessor once per process."""
    from vetinari.constraints.registry import get_constraint_registry

    return get_constraint_registry


@cache
def _get_blackboard_fn() -> Callable[[], Any]:
    """Load the blackboard accessor once per process."""
    from vetinari.memory.blackboard import get_blackboard

    return get_blackboard


def _get_execution_context_api() -> tuple[Any, Callable[..., Any], Callable[[], Any]]:
    """Return current execution-context permission helpers.

    The execution-context module is monkeypatched by regression tests and can
    be reconfigured by host tooling, so keep the module import cheap but do not
    cache function objects across calls.
    """
    runner_module = sys.modules.get("vetinari.orchestration.graph_task_runner")
    runner_override = getattr(runner_module, "_get_execution_context_api", None)
    if callable(runner_override) and runner_override is not _get_execution_context_api:
        return runner_override()

    from vetinari.execution_context import ToolPermission, enforce_agent_permissions, get_context_manager

    return ToolPermission, enforce_agent_permissions, get_context_manager


@cache
def _get_context_condenser_fn() -> Callable[[], Any]:
    """Load the context condenser accessor once per process."""
    from vetinari.context import get_context_condenser

    return get_context_condenser


@cache
def _get_task_manifest_context_cls() -> type[Any]:
    """Load the task manifest context class once per process."""
    from vetinari.orchestration.task_context import TaskManifestContext

    return TaskManifestContext


@cache
def _get_circuit_breaker_cls() -> type[Any]:
    """Load the circuit breaker class once per process."""
    from vetinari.resilience import CircuitBreaker

    return CircuitBreaker


@cache
def _get_agent_circuit_breaker_cls() -> type[Any]:
    """Load the per-agent circuit breaker class once per process."""
    from vetinari.agents.agent_circuit_breaker import AgentCircuitBreaker

    return AgentCircuitBreaker


@cache
def _get_cost_predictor_cls() -> type[Any]:
    """Load the cost predictor class once per process."""
    from vetinari.analytics.cost_predictor import CostPredictor

    return CostPredictor


@cache
def _get_policy_enforcer_fn() -> Callable[[], Any]:
    """Load the policy enforcer accessor once per process."""
    from vetinari.safety.policy_enforcer import get_policy_enforcer

    return get_policy_enforcer


@cache
def _get_agent_monitor_fn() -> Callable[[], Any]:
    """Load the agent monitor accessor once per process."""
    from vetinari.safety.agent_monitor import get_agent_monitor

    return get_agent_monitor
