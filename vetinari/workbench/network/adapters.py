"""Pure adapter projections for network transport decisions."""

from __future__ import annotations

from typing import Any

from vetinari.workbench.network.contracts import NetworkRecommendation, NetworkRoutingDecision


def routing_decision_to_tool_card(decision: NetworkRoutingDecision) -> dict[str, Any]:
    """Project a routing decision to a source/tool-card compatible payload."""
    return {
        "schema_version": "workbench-network-tool-card.v1",
        "tool": "network_transport_optimizer",
        "freshness": "ready" if not decision.refresh_connectors else "refresh-recommended",
        "decision": decision.to_dict(),
    }


def recommendation_to_approval_payload(recommendation: NetworkRecommendation) -> dict[str, Any]:
    """Project a risky recommendation to a non-authoritative approval payload.

    Returns:
        dict[str, Any] value produced by recommendation_to_approval_payload().
    """
    payload = recommendation.to_dict()
    payload["authority"] = "caller-owned"
    payload["may_execute"] = False
    return payload


__all__ = ["recommendation_to_approval_payload", "routing_decision_to_tool_card"]
