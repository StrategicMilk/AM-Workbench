"""Consolidated Operations - vetinari.tools.consolidated_operations.

High-level operations that combine multiple tool calls into a single function
call, reducing round-trips for common preparation and investigation patterns.

This is a convenience layer in the tool pipeline:
Intake -> **Consolidated Ops** -> Agent Execution -> Quality Gate -> Assembly

Two operations are provided:

- ``prepare_model(model_id, task_type)`` - replaces separate model-selection
  and config-lookup calls with one call that returns ready-to-use parameters.
- ``investigate_task(description, project_id)`` - replaces separate memory
  recall, skill search, and complexity-assessment calls with one call that
  returns combined context.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Complexity heuristics
# ---------------------------------------------------------------------------

# Keywords that each add weight toward "complex" classification.
_COMPLEXITY_TECHNICAL_TERMS: frozenset[str] = frozenset({
    "architecture",
    "distributed",
    "concurrent",
    "async",
    "pipeline",
    "orchestrat",
    "inference",
    "embed",
    "vector",
    "database",
    "migration",
    "refactor",
    "optimiz",
    "benchmark",
    "security",
    "authentication",
    "authoriz",
    "integrat",
    "deploy",
    "containeriz",
    "kubernetes",
    "schema",
    "transform",
    "aggregat",
    "multimodal",
    "training",
    "fine-tun",
})

# Phrases that indicate multi-step, compound tasks.
_MULTI_STEP_INDICATORS: tuple[str, ...] = (
    " and ",
    " then ",
    " after ",
    " before ",
    " followed by ",
    "first ",
    "finally ",
    "step ",
    "phase ",
    "multiple ",
    "several ",
    "across ",
    "end-to-end",
)

_SIMPLE_WORD_THRESHOLD = 8  # word count at or below -> "simple"
_COMPLEX_TERM_THRESHOLD = 3  # technical-term hits at or above -> "complex"


def _estimate_complexity(description: str) -> str:
    """Classify task complexity from its description using keyword heuristics.

    Uses three signals: word count, technical-term density, and presence of
    multi-step indicator phrases.  The classification is intentionally simple
    so it degrades gracefully and is fast to compute.

    Args:
        description: Free-text task description to classify.

    Returns:
        One of ``"simple"``, ``"moderate"``, or ``"complex"``.
    """
    words = description.lower().split()
    word_count = len(words)

    if word_count <= _SIMPLE_WORD_THRESHOLD:
        return "simple"

    # Count how many technical terms appear anywhere in the description.
    desc_lower = description.lower()
    tech_hits = sum(1 for term in _COMPLEXITY_TECHNICAL_TERMS if term in desc_lower)

    # Count multi-step indicator phrases.
    step_hits = sum(1 for phrase in _MULTI_STEP_INDICATORS if phrase in desc_lower)

    if tech_hits >= _COMPLEX_TERM_THRESHOLD or step_hits >= 2:
        return "complex"
    if tech_hits >= 1 or step_hits >= 1 or word_count >= 25:
        return "moderate"
    return "simple"


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PrepareModelResult:
    """Result of a consolidated model-preparation operation.

    Bundles the resolved model identifier with its recommended inference
    parameters so callers need only one function call instead of a separate
    model-selection call followed by a config-lookup call.

    Attributes:
        model_id: The model identifier that was prepared.
        task_type: The task type used to look up inference parameters.
        recommended_temperature: Sampling temperature drawn from the task
            inference profile (or a safe default when config is unavailable).
        recommended_max_tokens: Max-token budget drawn from the task inference
            profile (or a safe default).
        is_ready: ``True`` when configuration loaded successfully.
        notes: Human-readable explanation when ``is_ready`` is ``False``.
    """

    model_id: str
    task_type: str
    recommended_temperature: float
    recommended_max_tokens: int
    is_ready: bool
    notes: str = ""

    def __repr__(self) -> str:
        return (
            f"PrepareModelResult(model_id={self.model_id!r}, task_type={self.task_type!r}, is_ready={self.is_ready!r})"
        )


@dataclass(frozen=True, slots=True)
class InvestigateTaskResult:
    """Result of a consolidated task-investigation operation.

    Combines memory recall, skill matching, and complexity estimation into one
    object so callers avoid three separate subsystem calls.

    Attributes:
        description: The original task description that was investigated.
        relevant_memories: Content strings from the top matching memory
            entries.  Empty when memory subsystem is unavailable.
        matching_skills: Skill IDs whose capabilities overlap with keywords
            found in the description.  Empty when skill registry is
            unavailable.
        estimated_complexity: Heuristic complexity class - ``"simple"``,
            ``"moderate"``, or ``"complex"``.
        context_summary: Single-sentence summary combining the above signals,
            suitable for injecting into an agent system prompt.
    """

    description: str
    relevant_memories: list[str]
    matching_skills: list[str]
    estimated_complexity: str  # "simple" | "moderate" | "complex"
    context_summary: str

    def __repr__(self) -> str:
        return (
            f"InvestigateTaskResult(estimated_complexity={self.estimated_complexity!r}, "
            f"memories={len(self.relevant_memories)}, "
            f"skills={len(self.matching_skills)})"
        )


# ---------------------------------------------------------------------------
# Consolidated operations
# ---------------------------------------------------------------------------

# Safe fallback inference defaults used when config is unavailable.
_DEFAULT_TEMPERATURE: float = 0.3
_DEFAULT_MAX_TOKENS: int = 2048

# Memory search limit for investigation queries.
_MEMORY_SEARCH_LIMIT: int = 5


def prepare_model(model_id: str, task_type: str = "general") -> PrepareModelResult:
    """Consolidate model selection and inference-config lookup into one call.

    Replaces the pattern of:
    1. ``select_model(task_type)``
    2. ``InferenceConfigManager().get_effective_params(task_type, model_id)``

    with a single function call that returns a fully populated
    ``PrepareModelResult``.

    The function attempts to load task-specific inference parameters from
    ``InferenceConfigManager``.  If the config file is missing or the manager
    raises, the result degrades gracefully to safe defaults and
    ``is_ready=False``.

    No actual inference is performed - this is a preparation and configuration
    step only.

    Args:
        model_id: Identifier for the model to prepare (e.g.
            ``"qwen2.5-coder-7b"``).
        task_type: Task profile key used to look up inference parameters
            (e.g. ``"coding"``, ``"reasoning"``, ``"general"``).

    Returns:
        ``PrepareModelResult`` with resolved temperature, max_tokens, and
        readiness status.
    """
    try:
        from vetinari.config.inference_config import get_inference_config

        cfg = get_inference_config()
        params = cfg.get_effective_params(task_type, model_id)

        temperature = float(params.get("temperature", _DEFAULT_TEMPERATURE))
        max_tokens = int(params.get("max_tokens", _DEFAULT_MAX_TOKENS))

        logger.debug(
            "prepare_model: model=%s task=%s temperature=%s max_tokens=%s",
            model_id,
            task_type,
            temperature,
            max_tokens,
        )

        return PrepareModelResult(
            model_id=model_id,
            task_type=task_type,
            recommended_temperature=temperature,
            recommended_max_tokens=max_tokens,
            is_ready=True,
        )

    except Exception as exc:
        logger.warning(
            "prepare_model: could not load inference config for model %s task %s"
            " - using defaults (temperature=%.2f, max_tokens=%d)",
            model_id,
            task_type,
            _DEFAULT_TEMPERATURE,
            _DEFAULT_MAX_TOKENS,
        )
        return PrepareModelResult(
            model_id=model_id,
            task_type=task_type,
            recommended_temperature=_DEFAULT_TEMPERATURE,
            recommended_max_tokens=_DEFAULT_MAX_TOKENS,
            is_ready=False,
            notes=f"Inference config unavailable - {exc}",
        )


def investigate_task(
    description: str,
    project_id: str | None = None,
) -> InvestigateTaskResult:
    """Consolidate memory recall, skill search, and complexity estimation into one call.

    Replaces the pattern of:
    1. ``recall_memory(query=description)``
    2. ``skill_registry.get_all_skills()`` + keyword filter
    3. ``_estimate_complexity(description)``

    with a single function call that returns a fully populated
    ``InvestigateTaskResult``.

    Each subsystem is accessed inside a try/except so that failures degrade
    gracefully: an unavailable memory store yields an empty memories list; an
    unavailable skill registry yields an empty skills list.  Complexity
    estimation is pure-Python and never raises.

    Args:
        description: Free-text description of the task to investigate.
        project_id: Optional project scope for memory search.  When provided,
            passed as agent context to the memory store.  Currently informational
            only - the memory store does not filter by project_id directly.

    Returns:
        ``InvestigateTaskResult`` with memories, matching skills, complexity
        estimate, and a context summary string.
    """
    relevant_memories = _recall_task_memories(description, project_id)
    matching_skills = _match_task_skills(description)

    # -- 3. Complexity estimation ------------------------------------------------
    estimated_complexity = _estimate_complexity(description)

    # -- 4. Context summary -----------------------------------------------------
    memory_note = f"{len(relevant_memories)} prior memory entries found" if relevant_memories else "no prior memories"
    skill_note = f"matching skills: {', '.join(matching_skills)}" if matching_skills else "no skill matches"
    context_summary = f"Task complexity: {estimated_complexity}. {memory_note}. {skill_note}."

    return InvestigateTaskResult(
        description=description,
        relevant_memories=relevant_memories,
        matching_skills=matching_skills,
        estimated_complexity=estimated_complexity,
        context_summary=context_summary,
    )


def _recall_task_memories(description: str, project_id: str | None) -> list[str]:
    """Return relevant memory entries for a task description."""
    try:
        from vetinari.memory import get_unified_memory_store

        entries = get_unified_memory_store().search(query=description, limit=_MEMORY_SEARCH_LIMIT)
        memories = [entry.content for entry in entries if entry.content]
        logger.debug("investigate_task: memory search returned %d entries for project=%s", len(memories), project_id)
        return memories
    except Exception:
        logger.warning("investigate_task: memory subsystem unavailable for description %r", description[:80])
        return []


def _match_task_skills(description: str) -> list[str]:
    """Return skill ids whose capabilities or tags match a task description."""
    try:
        from vetinari.skills.skill_registry import get_all_skills

        desc_lower = description.lower()
        matches = []
        for skill_id, spec in get_all_skills().items():
            if any(cap.lower() in desc_lower for cap in spec.capabilities) or any(
                tag.lower() in desc_lower for tag in spec.tags
            ):
                matches.append(skill_id)
        logger.debug("investigate_task: %d matching skills for description %r", len(matches), description[:80])
        return matches
    except Exception:
        logger.warning("investigate_task: skill registry unavailable for description %r", description[:80])
        return []
