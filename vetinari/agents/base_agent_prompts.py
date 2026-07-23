"""Prompt framework constants and system prompt assembly for BaseAgent.

Contains prompt-related code extracted from ``base_agent.py`` to keep
that file under the 550-line limit:

- ``BASE_PROMPT_FRAMEWORK``: full prompt framework string for large models (>7B)
- ``COMPACT_PROMPT_FRAMEWORK``: compact variant for small models (<=7B)
- ``SYSTEM_PROMPT_BOUNDARY``: cache boundary marker separating stable from dynamic content
- ``build_system_prompt(agent, mode)``: tiered system prompt assembly with
  identity, practices, standards, rules, and RAG knowledge injection
- ``split_at_boundary(prompt)``: split a prompt into stable and dynamic halves
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from vetinari.agents.base_agent import BaseAgent


# Full prompt framework for large models (>7B parameters).
# Covers reasoning steps, confidence rating, verification, error handling,
# and quality standards (~250 tokens).
BASE_PROMPT_FRAMEWORK = (
    "## Core Operating Principles\n\n"
    "REASONING: Think step-by-step before producing output. For complex decisions:\n"
    "1. Identify the key question or requirement\n"
    "2. Consider 2-3 approaches with trade-offs\n"
    "3. Choose the best approach and explain why\n"
    "4. Execute with verification\n\n"
    "CONFIDENCE: Rate your confidence in each major output:\n"
    "- HIGH (>80%): Well-understood domain, clear requirements, verified data\n"
    "- MEDIUM (50-80%): Some ambiguity, partial information, reasonable inference\n"
    "- LOW (<50%): Significant uncertainty — flag for human review\n\n"
    "VERIFICATION: Before finalizing output:\n"
    "- Does this directly address the task requirements?\n"
    "- Are there logical contradictions or unsupported claims?\n"
    "- Would a domain expert find obvious errors?\n"
    "- Is the output format correct and complete?\n\n"
    "ERROR HANDLING:\n"
    "- If requirements are ambiguous, state your assumptions explicitly\n"
    "- If you lack information, say so rather than fabricating data\n"
    "- If a subtask fails, provide partial results with clear error context\n"
    "- Never silently drop errors — always surface them\n\n"
    "QUALITY:\n"
    "- Cite sources or reasoning for factual claims\n"
    "- Prefer specific, actionable output over vague generalities\n"
    "- If output exceeds expected scope, summarize and offer details on request\n"
    "- Maintain consistent terminology throughout\n\n"
    "## Operating Standards\n\n"
    "INTEGRITY: Always do things correctly, even when harder or slower. "
    "No shortcuts, no placeholders, no superficial patches. "
    "Fix root causes. Correctness is not negotiable.\n"
    "SCOPE: Only address what the task requires. Do not add unrequested extras."
)

# Compact variant for small models (<=7B parameters).
# ~60 tokens — avoids flooding a small context window with instructions.
COMPACT_PROMPT_FRAMEWORK = (
    "## Instructions\n\n"
    "Think step-by-step. Rate confidence: HIGH/MEDIUM/LOW.\n"
    "If uncertain, say so rather than guessing.\n"
    "Verify output addresses requirements before finalizing.\n\n"
    "INTEGRITY: Do things correctly. No shortcuts or placeholders. "
    "Fix root causes. Only address what the task requires.\n"
)


# Marker separating stable system prompt content from dynamic content.
# Everything BEFORE this marker is cacheable in the KV cache across requests
# within a pipeline run. Everything AFTER changes per-request (task context,
# conversation history, RAG results). llama-cpp-python reuses the KV cache
# for the stable prefix automatically when subsequent requests share it.
SYSTEM_PROMPT_BOUNDARY = "\n\n<!-- SYSTEM_PROMPT_DYNAMIC_BOUNDARY -->\n\n"


def split_at_boundary(prompt: str) -> tuple[str, str]:
    """Split a system prompt into stable prefix and dynamic suffix at the cache boundary.

    The stable prefix is identical across requests within a pipeline run and can
    be cached in the KV cache. The dynamic suffix changes per-request (task
    context, conversation history, RAG results).

    Args:
        prompt: Full system prompt potentially containing SYSTEM_PROMPT_BOUNDARY.

    Returns:
        Tuple of (stable_prefix, dynamic_suffix). If no boundary is present,
        the entire prompt is returned as stable with an empty dynamic suffix.
    """
    if SYSTEM_PROMPT_BOUNDARY in prompt:
        stable, dynamic = prompt.split(SYSTEM_PROMPT_BOUNDARY, maxsplit=1)
        return stable, dynamic
    return prompt, ""


def build_system_prompt(agent: BaseAgent, mode: str = "") -> str:
    """Build a tiered system prompt with selective context injection.

    Assembles the prompt from four tiers:
    - Tier 1: Core correctness principle (~20 tokens)
    - Tier 2: Agent identity from ``get_system_prompt()``
    - Tier 3: Mode-relevant practices, standards, and rules
    - Tier 4: RAG knowledge base context (if available)

    Args:
        agent: The BaseAgent instance providing identity and type info.
        mode: Current agent mode for context-relevant tier injection.

    Returns:
        Complete assembled system prompt string.
    """
    # Stable parts: identical across requests for the same agent type and mode.
    # These are safe to cache in the KV cache.
    stable_parts: list[str] = []

    # Dynamic parts: change per-request (RAG results, task context).
    # These go after the boundary marker and bypass KV cache reuse.
    dynamic_parts: list[str] = []

    # Tier 1: Core principles
    stable_parts.append(
        "## Core Principles\n\n"
        "Correctness above all. Never fabricate, never skip verification, "
        "never declare done without evidence.\n",
    )

    # Agent identity
    try:
        identity = agent.get_system_prompt()
        if identity:
            stable_parts.append(identity)
    except Exception:
        logger.warning("Could not get system prompt for context assembly", exc_info=True)

    _append_mode_context(stable_parts, agent, mode)

    # Tier 3: RAG knowledge base context (if available) — dynamic because results
    # vary by query and change as the knowledge base is updated.
    _append_rag_context(dynamic_parts, agent, mode)

    # Separate stable and dynamic parts for KV cache reuse
    stable = "\n\n".join(stable_parts)
    if dynamic_parts:
        dynamic = "\n\n".join(dynamic_parts)
        combined = stable + SYSTEM_PROMPT_BOUNDARY + dynamic
    else:
        combined = stable
    ctx_hash = hashlib.sha256(combined.encode()).hexdigest()[:12]
    logger.info(
        "System prompt assembled for %s mode=%s hash=%s tokens≈%d",
        agent.agent_type.value,
        mode,
        ctx_hash,
        len(combined) // 4,
    )

    return combined


def _append_mode_context(stable_parts: list[str], agent: BaseAgent, mode: str) -> None:
    try:
        from vetinari.agents.practices import get_practices_for_mode

        practices = get_practices_for_mode(mode)
        if practices:
            stable_parts.append(practices)
    except Exception:
        logger.warning("Could not load practices for mode %s", mode, exc_info=True)

    try:
        from vetinari.config.standards_loader import get_standards_loader

        standards = get_standards_loader().get_for_context(agent.agent_type.value, mode)
        if standards:
            stable_parts.append(standards)
    except Exception:
        logger.warning("Could not load standards for mode %s", mode, exc_info=True)

    try:
        from vetinari.rules_manager import get_rules_manager

        rules = get_rules_manager().format_rules_for_context(agent.agent_type.value, mode)
        if rules:
            stable_parts.append(rules)
    except Exception:
        logger.warning("Could not load rules for mode %s", mode, exc_info=True)


def _append_rag_context(dynamic_parts: list[str], agent: BaseAgent, mode: str) -> None:
    try:
        from vetinari.rag import get_knowledge_base

        kb = get_knowledge_base()
        if kb is None or not mode:
            return
        kb_results = kb.query(f"{agent.agent_type.value} {mode}", k=3)
        kb_context = "\n".join(f"- {doc.content[:200]}" for doc in kb_results if doc.content)
        if kb_context:
            dynamic_parts.append(f"## Relevant Knowledge\n\n{kb_context}")
    except Exception:
        logger.warning("RAG knowledge base unavailable for context enrichment", exc_info=True)
