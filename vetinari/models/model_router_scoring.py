"""Pure scoring helpers for DynamicModelRouter.

Stateless functions extracted from ``dynamic_model_router.py`` so the router
class stays under the 500-line god-class limit.  These functions have no
side-effects and do not require access to ``self``.

Exported symbols are re-exported from ``dynamic_model_router`` for backward
compatibility.
"""

from __future__ import annotations

import logging
import re

from vetinari.models.model_router_types import RouterModelInfo, TaskType
from vetinari.types import GoalCategory, ModelProvider

logger = logging.getLogger(__name__)


__all__ = [
    "assess_difficulty",
    "assess_warm_model_bonus",
    "calculate_confidence",
    "generate_reasoning",
    "infer_task_type",
    "parse_model_size_b",
]


def assess_warm_model_bonus(model_id: str) -> float:
    """Return a scoring bonus if the model is already loaded in VRAM.

    A warm model has zero load latency and avoids VRAM churn from evicting
    another model.  The bonus (0.12) is large enough to prefer a loaded model
    over a marginally better unloaded one, but small enough that a clearly
    superior model still wins.  Decision: ADR-0087.

    Args:
        model_id: Model identifier to check.

    Returns:
        0.12 if the model is loaded, 0.0 otherwise.
    """
    try:
        from vetinari.models.vram_manager import get_vram_manager

        mgr = get_vram_manager()
        # _estimates is written under _lock_rw — hold the lock for the read to
        # avoid a TOCTOU race with concurrent load/evict operations.
        with mgr._lock_rw:
            is_loaded = model_id in mgr._estimates
        if is_loaded:
            return 0.12
    except Exception:
        logger.warning("VRAMManager unavailable for warm model check of %s — warm model bonus not applied", model_id)
    return 0.0


def parse_model_size_b(model_id: str) -> float:
    """Parse model size in billions of parameters from a model ID string.

    Recognises patterns like ``"qwen-14b"``, ``"llama-3-70b-instruct"``,
    ``"mistral-7b-v0.3"``.  Returns 0.0 when no size can be parsed so that
    callers can safely compare ``parse_model_size_b(m.id) >= 30`` without
    special-casing the unknown case.

    Args:
        model_id: Model identifier string, e.g. ``"qwen2.5-72b-instruct"``.

    Returns:
        Size in billions (float), or 0.0 if not found.
    """
    match = re.search(r"(\d+(?:\.\d+)?)\s*b(?:\b|[-_])", model_id.lower())
    if match:
        return float(match.group(1))
    return 0.0


def assess_difficulty(
    task_description: str,
    task_type: str = "general",
    calibration_bias: float = 0.0,
) -> float:
    """Assess task difficulty to inform model selection.

    Uses heuristic signals from the task description to estimate complexity
    on a 0.0-1.0 scale.  Higher values bias toward larger, more capable models.
    An optional ``calibration_bias`` from historical prediction errors can nudge
    the result up or down based on past accuracy for this task type.

    Args:
        task_description: The task description to assess.
        task_type: Task type string for category-based difficulty adjustment.
        calibration_bias: Historical mean prediction error for this task type,
            sourced from ``get_calibration_bias()``. Positive values indicate
            the heuristic has historically underestimated difficulty.

    Returns:
        Difficulty score between 0.0 (trivial) and 1.0 (very complex).
    """
    score = 0.05  # Base difficulty — small epsilon so trivially easy tasks can score near 0.0
    desc_lower = task_description.lower()

    # Length signal: longer descriptions tend to be more complex
    if len(task_description) > 500:
        score += 0.1
    if len(task_description) > 1000:
        score += 0.1

    # Complexity keywords
    complexity_keywords = [
        "security",
        "vulnerability",
        "audit",
        "concurrent",
        "thread",
        "race condition",
        "deadlock",
        "architecture",
        "refactor",
        "migration",
        "performance",
        "optimize",
        "encryption",
    ]
    keyword_hits = sum(1 for kw in complexity_keywords if kw in desc_lower)
    score += min(keyword_hits * 0.05, 0.25)

    # High-stakes task types
    if task_type in {"security_audit", "security", "code_review", "testing"}:
        score += 0.15

    # Apply historical calibration: if past predictions underestimated difficulty
    # for this task_type, the bias is positive and nudges the score up.
    score += calibration_bias

    return min(max(score, 0.0), 1.0)


def calculate_confidence(scored: list[tuple]) -> float:
    """Calculate confidence in the model selection based on score distribution.

    A large gap between the top-scoring and second-scoring model indicates a
    clear winner; a small gap means the selection is uncertain.

    Args:
        scored: List of ``(model, score)`` tuples sorted descending by score.

    Returns:
        Confidence value between 0.0 and 1.0.
    """
    if len(scored) < 2:
        return 0.5

    best_score = scored[0][1]
    second_score = scored[1][1]

    if best_score == 0:
        return 0.1

    gap = best_score - second_score
    return min(1.0, gap * 2 + 0.3)


def generate_reasoning(model: RouterModelInfo, task_type: TaskType, score: float) -> str:
    """Generate human-readable reasoning for a model selection decision.

    Args:
        model: The selected ``RouterModelInfo`` instance.
        task_type: The task type (``GoalCategory``) the model was selected for.
        score: The numeric score that drove the selection.

    Returns:
        A short human-readable string explaining why this model was chosen.
    """
    reasons: list[str] = []

    caps = model.capabilities
    if task_type == GoalCategory.CODE and caps.code_gen:
        reasons.append("excellent code generation")
    elif task_type == GoalCategory.REASONING and caps.reasoning:
        reasons.append("strong reasoning capabilities")
    elif task_type == GoalCategory.DOCS and caps.docs:
        reasons.append("good documentation skills")

    if model.total_uses > 10:
        reasons.append(f"proven track record ({model.total_uses} uses)")
    if model.avg_latency_ms > 0 and model.avg_latency_ms < 5000:
        reasons.append(f"fast response ({model.avg_latency_ms:.0f}ms)")

    if model.provider == ModelProvider.LOCAL:
        reasons.append("local model (no API costs)")

    if not reasons:
        reasons.append("best available match")

    return f"Selected {model.id}: {', '.join(reasons)}"


def infer_task_type(description: str) -> GoalCategory:
    """Infer task type from a free-text description using keyword matching.

    Order matters — more specific categories are checked before general ones
    so that ``"security audit"`` matches SECURITY, not RESEARCH.

    Args:
        description: Free-text task description.

    Returns:
        The best-matching GoalCategory.
    """
    desc_lower = description.lower()

    # Specific categories first
    if any(kw in desc_lower for kw in ["security", "audit", "vulnerability", "pentest", "cve", "owasp"]):
        return GoalCategory.SECURITY
    if any(kw in desc_lower for kw in ["deploy", "ci/cd", "docker", "kubernetes", "pipeline", "devops", "terraform"]):
        return GoalCategory.DEVOPS
    if any(kw in desc_lower for kw in ["logo", "icon", "mockup", "diagram", "image", "illustration"]):
        return GoalCategory.IMAGE
    if any(kw in desc_lower for kw in ["cost", "budget", "pricing", "expense", "billing"]):
        return GoalCategory.COST_ANALYSIS
    if any(kw in desc_lower for kw in ["specification", "spec", "requirements", "acceptance criteria"]):
        return GoalCategory.SPECIFICATION
    if any(kw in desc_lower for kw in ["story", "poem", "fiction", "narrative", "campaign", "creative writ"]):
        return GoalCategory.CREATIVE
    # Broader categories
    if any(kw in desc_lower for kw in ["plan", "strategy", "workflow", "design", "architect"]):
        return GoalCategory.PLANNING
    if any(kw in desc_lower for kw in ["analyze", "analysis", "research", "investigate"]):
        return GoalCategory.RESEARCH
    if any(kw in desc_lower for kw in ["code", "implement", "build", "create", "program", "function", "class"]):
        return GoalCategory.CODE
    if any(kw in desc_lower for kw in ["review", "refactor", "improve", "optimize"]):
        return GoalCategory.CODE_REVIEW
    if any(kw in desc_lower for kw in ["test", "testing", "assert"]):
        return GoalCategory.TESTING
    if any(kw in desc_lower for kw in ["document", "readme", "docs", "comment", "explain"]):
        return GoalCategory.DOCS
    if any(kw in desc_lower for kw in ["reason", "logic", "solve", "problem", "math"]):
        return GoalCategory.REASONING
    if any(kw in desc_lower for kw in ["creative", "write", "article"]):
        return GoalCategory.CREATIVE
    if any(kw in desc_lower for kw in ["data", "process", "extract", "transform", "etl", "database", "schema", "sql"]):
        return GoalCategory.DATA
    if any(kw in desc_lower for kw in ["search", "find", "look", "query", "web"]):
        return GoalCategory.WEB_SEARCH
    if any(kw in desc_lower for kw in ["summarize", "summary", "condense"]):
        return GoalCategory.SUMMARIZATION
    if any(kw in desc_lower for kw in ["translate", "translation", "convert"]):
        return GoalCategory.TRANSLATION
    return GoalCategory.GENERAL
