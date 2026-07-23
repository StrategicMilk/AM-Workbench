"""Routing subsystem — model routing, complexity analysis, topology selection."""

from __future__ import annotations

from vetinari.routing.complexity_router import (
    Complexity,
    RoutingDecision,
    classify_complexity,
    route_by_complexity,
)
from vetinari.routing.dag_analyzer import DAGShape, analyze_dag, suggest_topology
from vetinari.routing.system_router import (
    InspectorBypassCheck,
    ModelTier,
    SystemDecision,
    SystemType,
    check_inspector_bypass_safety,
    get_system_routing_stats,
    route_system,
)
from vetinari.routing.topology_router import (
    Topology,
    TopologyDecision,
    TopologyRouter,
)

__all__ = [
    "Complexity",
    "DAGShape",
    "InspectorBypassCheck",
    "ModelTier",
    "RoutingDecision",
    "SystemDecision",
    "SystemType",
    "Topology",
    "TopologyDecision",
    "TopologyRouter",
    "analyze_dag",
    "check_inspector_bypass_safety",
    "classify_complexity",
    "get_system_routing_stats",
    "route_by_complexity",
    "route_system",
    "suggest_topology",
]
