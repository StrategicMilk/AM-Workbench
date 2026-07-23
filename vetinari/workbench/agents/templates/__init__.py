"""Workbench agent template gallery contract surface."""

from __future__ import annotations

from vetinari.workbench.agents.templates.cards import (
    AgentTemplateCard,
    AgentTemplateCatalogError,
    AgentTemplateRiskPosture,
    AgentTemplateTrustBadges,
    load_agent_template_gallery,
    reset_agent_template_gallery_for_test,
)
from vetinari.workbench.agents.templates.contracts import (
    AgentHandoffEnvelope,
    AgentSpawnProjection,
    AgentSpawnRequest,
    AgentTemplateContractError,
    project_spawn_request,
    render_mission_control_handoff_payload,
)

__all__ = [
    "AgentHandoffEnvelope",
    "AgentSpawnProjection",
    "AgentSpawnRequest",
    "AgentTemplateCard",
    "AgentTemplateCatalogError",
    "AgentTemplateContractError",
    "AgentTemplateRiskPosture",
    "AgentTemplateTrustBadges",
    "load_agent_template_gallery",
    "project_spawn_request",
    "render_mission_control_handoff_payload",
    "reset_agent_template_gallery_for_test",
]
