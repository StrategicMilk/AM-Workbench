"""A2A Agent Card definitions for Vetinari's factory pipeline agents.

Google's Agent-to-Agent (A2A) protocol uses agent cards — JSON-serialisable
descriptors — to advertise an agent's identity, capabilities, accepted input
types, produced output types, and available skills to external callers.

This module defines one card for each of the three Vetinari agent roles:
Foreman, Worker, and Inspector.  Worker skills are computed from
``WorkerAgent.MODES`` and ``MODE_GROUPS`` at call time so the card never
drifts from the live mode count.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from vetinari.constants import DEFAULT_A2A_BASE_URL
from vetinari.types import AgentType

logger = logging.getLogger(__name__)


# ── Configuration ─────────────────────────────────────────────────────────────

_CARD_VERSION = "1.0.0"  # A2A card schema version exported by this module
_A2A_BASE_URL = DEFAULT_A2A_BASE_URL

# Single shared A2A endpoint — the per-agent path segments do not exist.
# All three cards point here so external callers use the real mounted route.
_A2A_ENDPOINT = f"{_A2A_BASE_URL}/api/v1/a2a"
_A2A_PROTOCOL_VERSION = "a2a-0.2"
_A2A_TRANSPORT = "jsonrpc"


def _market_response_metadata(agent_type: AgentType) -> dict[str, str]:
    """Return live A2A metadata that states local-first market posture."""
    return {
        "a2aCardVersion": _CARD_VERSION,
        "agentType": agent_type.value,
        "dataBoundary": "local-first",
        "deployment": "single-user-localhost",
        "qualityGate": "inspector-pass-fail",
        "runtimePipeline": "Foreman -> Worker -> Inspector",
    }


# ── Worker skill descriptions ─────────────────────────────────────────────────
# Maps each WorkerAgent mode name to a short human-readable description.
# Kept as a module-level constant so descriptions can be reviewed and updated
# without touching the card factory logic.
# If a mode is missing from this map, a generic description is derived at
# runtime — see get_worker_card() for the derivation rule.
#
# Who writes: this constant (manual maintenance).
# Who reads:  get_worker_card() — constructs the skill list from MODES + this map.
_WORKER_SKILL_DESCRIPTIONS: dict[str, str] = {
    # Research group
    "code_discovery": "Explore and map an existing codebase.",
    "domain_research": "Research a domain or technology area.",
    "api_lookup": "Look up API documentation and usage patterns.",
    "lateral_thinking": "Generate creative alternative solutions.",
    "ui_design": "Research UI/UX patterns and produce design specs.",
    "database": "Research database schemas and query patterns.",
    "devops": "Research DevOps pipelines and infrastructure patterns.",
    "git_workflow": "Research and advise on git branching and workflow strategies.",
    # Architecture group
    "architecture": "Produce architecture designs and ADRs.",
    "risk_assessment": "Assess technical and security risks.",
    "ontological_analysis": "Model domain concepts and relationships.",
    "contrarian_review": "Challenge assumptions in a proposed design.",
    "suggest": "Suggest improvements to an existing design.",
    # Build group
    "build": "Implement production code from a task specification.",
    "image_generation": "Generate images and visual assets.",
    # Operations group
    "documentation": "Write technical documentation and docstrings.",
    "creative_writing": "Produce creative prose, copy, or narrative.",
    "cost_analysis": "Analyse token and infrastructure costs.",
    "experiment": "Design and run controlled experiments.",
    "error_recovery": "Diagnose and recover from execution errors.",
    "synthesis": "Synthesise findings from multiple sources.",
    "improvement": "Identify and implement incremental improvements.",
    "monitor": "Monitor running systems and report anomalies.",
    "devops_ops": "Execute DevOps operations and deployments.",
}


# ── AgentCard dataclass ──────────────────────────────────────────────────────


@dataclass
class AgentCard:
    """A2A protocol descriptor that advertises a Vetinari agent to the network.

    An AgentCard is the machine-readable identity document for one agent.
    External callers use it to discover what an agent can do, which A2A task
    types it accepts, and how to reach it.

    Attributes:
        name: Human-readable agent name.
        description: One-sentence summary of the agent's role.
        url: Base URL at which the agent's A2A endpoint is reachable.
        version: Semantic version string for this card.
        capabilities: List of high-level capability labels.
        supported_input_types: MIME types or structured labels this agent
            accepts as input.
        supported_output_types: MIME types or structured labels this agent
            produces as output.
        skills: List of skill descriptors, each a dict with at minimum an
            ``id`` and ``name`` key.
        agent_type: The internal :class:`~vetinari.types.AgentType` enum
            value that this card represents.
    """

    name: str
    description: str
    url: str
    version: str
    capabilities: list[str]
    supported_input_types: list[str]
    supported_output_types: list[str]
    skills: list[dict]
    agent_type: AgentType = AgentType.FOREMAN
    protocol_version: str = _A2A_PROTOCOL_VERSION
    preferred_transport: str = _A2A_TRANSPORT
    metadata: dict[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"AgentCard(name={self.name!r}, agent_type={self.agent_type!r}, url={self.url!r})"

    def to_dict(self) -> dict:
        """Serialise this card to a plain dict suitable for JSON encoding.

        Returns:
            Dictionary representation of the agent card.
        """
        return {
            "name": self.name,
            "description": self.description,
            "url": self.url,
            "version": self.version,
            "capabilities": self.capabilities,
            "supportedInputTypes": self.supported_input_types,
            "supportedOutputTypes": self.supported_output_types,
            "skills": self.skills,
            "agentType": self.agent_type.value,
            "protocolVersion": self.protocol_version,
            "preferredTransport": self.preferred_transport,
            "metadata": self.metadata or _market_response_metadata(self.agent_type),
        }


# ── Card factory functions ───────────────────────────────────────────────────


def get_foreman_card() -> AgentCard:
    """Return the A2A AgentCard for the Foreman agent.

    The Foreman is the factory pipeline orchestrator: it decomposes user
    goals, manages task DAGs, and routes work to the Worker.  Its A2A
    skills map to its operating modes.

    Returns:
        Populated :class:`AgentCard` for the Foreman agent.
    """
    logger.debug("Building Foreman A2A agent card")
    skills: list[dict] = [
        {
            "id": "foreman/plan",
            "name": "plan",
            "description": "Decompose a user goal into a structured task DAG.",
            "tags": ["planning", "decomposition"],
        },
        {
            "id": "foreman/clarify",
            "name": "clarify",
            "description": "Ask targeted clarifying questions before planning.",
            "tags": ["clarification"],
        },
        {
            "id": "foreman/consolidate",
            "name": "consolidate",
            "description": "Merge multiple task results into a coherent output.",
            "tags": ["synthesis"],
        },
        {
            "id": "foreman/summarise",
            "name": "summarise",
            "description": "Produce a concise summary of completed work.",
            "tags": ["summarisation"],
        },
        {
            "id": "foreman/prune",
            "name": "prune",
            "description": "Remove redundant or obsolete tasks from the active plan.",
            "tags": ["optimisation"],
        },
        {
            "id": "foreman/extract",
            "name": "extract",
            "description": "Extract structured data from unstructured plan artefacts.",
            "tags": ["extraction"],
        },
    ]
    num_skills = len(skills)
    return AgentCard(
        name="Vetinari Foreman",
        description=(
            f"Factory pipeline orchestrator: decomposes goals, manages task DAGs, "
            f"routes work to Worker agents, and drives plans to completion across {num_skills} operating modes."
        ),
        url=_A2A_ENDPOINT,
        version=_CARD_VERSION,
        agent_type=AgentType.FOREMAN,
        capabilities=[
            "goal_decomposition",
            "task_graph_management",
            "plan_lifecycle",
            "clarification",
            "consolidation",
            "summarisation",
            "pruning",
            "extraction",
        ],
        supported_input_types=["application/json"],
        supported_output_types=["application/json"],
        skills=skills,
        metadata=_market_response_metadata(AgentType.FOREMAN),
    )


def get_worker_card() -> AgentCard:
    """Return the A2A AgentCard for the Worker agent.

    Skills are computed at call time from ``WorkerAgent.MODES`` and
    ``MODE_GROUPS`` so this card is always consistent with the live agent
    implementation.  If ``WorkerAgent`` cannot be imported, an
    :class:`ImportError` is raised — there is no silent fallback to stale
    literals.

    Returns:
        Populated :class:`AgentCard` for the Worker agent.

    Raises:
        ImportError: If ``vetinari.agents.consolidated.worker_agent`` is
            unavailable.  Callers must handle this if the full agent package
            is not installed.
    """
    logger.debug("Building Worker A2A agent card from live WorkerAgent.MODES")

    # Lazy import to avoid loading the heavy agent subsystem at module import
    # time.  This module is imported early in the web layer, so we cannot pay
    # the cost of loading all agent dependencies at startup.
    try:
        from vetinari.agents.consolidated.worker_agent import MODE_GROUPS, WorkerAgent

        modes = WorkerAgent.MODES
        mode_groups = MODE_GROUPS
    except ImportError as exc:
        raise ImportError(
            "Cannot build Worker A2A card: vetinari.agents.consolidated.worker_agent is "
            "unavailable. Install the full agent package."
        ) from exc

    # Build skill dicts from live MODES + MODE_GROUPS.
    # For missing descriptions, fall back to a generic derived string so the
    # card is still usable even if _WORKER_SKILL_DESCRIPTIONS falls behind.
    all_skills: list[dict] = []
    for mode_name in modes:
        mode_group = mode_groups.get(mode_name, "operations")
        description = _WORKER_SKILL_DESCRIPTIONS.get(
            mode_name,
            f"Worker {mode_group} mode: {mode_name}",  # generic fallback for undocumented modes
        )
        all_skills.append({
            "id": f"worker/{mode_name}",
            "name": mode_name,
            "description": description,
            "tags": [mode_group],
        })

    num_modes = len(modes)
    unique_groups = sorted(set(mode_groups.values()))
    num_groups = len(unique_groups)

    return AgentCard(
        name="Vetinari Worker",
        description=(
            f"Unified execution agent: handles {', '.join(unique_groups)} tasks "
            f"across {num_modes} modes in {num_groups} mode groups."
        ),
        url=_A2A_ENDPOINT,
        version=_CARD_VERSION,
        agent_type=AgentType.WORKER,
        capabilities=[
            "code_execution",
            "research",
            "architecture",
            "build",
            "operations",
            "documentation",
        ],
        supported_input_types=["application/json"],
        supported_output_types=["application/json"],
        skills=all_skills,
        metadata=_market_response_metadata(AgentType.WORKER),
    )


def get_inspector_card() -> AgentCard:
    """Return the A2A AgentCard for the Inspector agent.

    The Inspector is Vetinari's independent quality gate.  It reviews Worker
    output and issues mandatory PASS/FAIL decisions.

    Returns:
        Populated :class:`AgentCard` for the Inspector agent.
    """
    logger.debug("Building Inspector A2A agent card")
    skills: list[dict] = [
        {
            "id": "inspector/code_review",
            "name": "code_review",
            "description": "Review code for correctness, style, and maintainability.",
            "tags": ["review"],
        },
        {
            "id": "inspector/security_audit",
            "name": "security_audit",
            "description": "Audit code for security vulnerabilities.",
            "tags": ["security"],
        },
        {
            "id": "inspector/test_generation",
            "name": "test_generation",
            "description": "Generate tests for existing or new code.",
            "tags": ["testing"],
        },
        {
            "id": "inspector/simplification",
            "name": "simplification",
            "description": "Simplify overly complex code or designs.",
            "tags": ["quality"],
        },
    ]
    num_skills = len(skills)
    return AgentCard(
        name="Vetinari Inspector",
        description=(
            f"Independent quality gate: reviews Worker output across {num_skills} modes "
            f"and issues mandatory PASS/FAIL gate decisions."
        ),
        url=_A2A_ENDPOINT,
        version=_CARD_VERSION,
        agent_type=AgentType.INSPECTOR,
        capabilities=[
            "code_review",
            "security_audit",
            "test_generation",
            "simplification",
        ],
        supported_input_types=["application/json"],
        supported_output_types=["application/json"],
        skills=skills,
        metadata=_market_response_metadata(AgentType.INSPECTOR),
    )


def get_all_cards() -> list[AgentCard]:
    """Return the A2A cards for all three Vetinari pipeline agents.

    Returns:
        List containing the Foreman, Worker, and Inspector cards in pipeline
        order: [Foreman, Worker, Inspector].
    """
    cards = [get_foreman_card(), get_worker_card(), get_inspector_card()]
    logger.debug("Returning %d A2A agent cards", len(cards))
    return cards
