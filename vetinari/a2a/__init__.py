"""Vetinari A2A (Agent-to-Agent) and AG-UI protocol stack.

Implements Google's Agent-to-Agent (A2A) protocol and CopilotKit's AG-UI
streaming protocol, enabling Vetinari to interoperate with external agent
runtimes and streaming UIs.

Exports:
    AgentCard: JSON descriptor for a Vetinari agent (A2A spec).
    VetinariA2AExecutor: Routes incoming A2A tasks to the internal pipeline.
    A2ATransport: HTTP/JSON-RPC transport layer for A2A messages.
    AGUIEventEmitter: Emits AG-UI streaming events to connected UIs.
    AGUIEventType: Enum of the 17 AG-UI event types.
"""

from __future__ import annotations

from vetinari.a2a.ag_ui import AGUIEventEmitter, AGUIEventType
from vetinari.a2a.agent_cards import AgentCard, get_all_cards, get_foreman_card, get_inspector_card, get_worker_card
from vetinari.a2a.executor import A2AResult, A2ATask, VetinariA2AExecutor
from vetinari.a2a.transport import A2ATransport

REQUIRED_A2A_EXPORTS = (
    "AgentCard",
    "VetinariA2AExecutor",
    "A2ATransport",
    "AGUIEventEmitter",
    "AGUIEventType",
)


def validate_a2a_exports(namespace: dict[str, object] | None = None) -> tuple[str, ...]:
    """Return required export names, failing closed if protocol wiring is incomplete.

    Args:
        namespace: Optional export namespace to validate. Defaults to this module's
            globals so startup checks exercise the real public A2A surface.

    Returns:
        Tuple of required A2A export names when every required object is present.

    Raises:
        RuntimeError: If any required A2A export is missing or bound to ``None``.
    """
    candidate = namespace if namespace is not None else globals()
    missing = [name for name in REQUIRED_A2A_EXPORTS if candidate.get(name) is None]
    if missing:
        raise RuntimeError(f"A2A protocol exports are incomplete: {', '.join(missing)}")
    return REQUIRED_A2A_EXPORTS


__all__ = [
    "REQUIRED_A2A_EXPORTS",
    "A2AResult",
    "A2ATask",
    "A2ATransport",
    "AGUIEventEmitter",
    "AGUIEventType",
    "AgentCard",
    "VetinariA2AExecutor",
    "get_all_cards",
    "get_foreman_card",
    "get_inspector_card",
    "get_worker_card",
    "validate_a2a_exports",
]
