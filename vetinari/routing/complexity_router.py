"""Dynamic Complexity Router — pipeline stage 0: intake classification.

Classifies tasks as simple/moderate/complex and routes them to appropriate
pipeline subsets.

Pipeline role:
    **ComplexityRouter** (classify) → Planning → Execution → Verify → Learn
    This is the first decision point after a request arrives.  The router
    inspects the request text and signals which pipeline variant to run,
    trading off latency against quality: simple requests skip expensive
    research and planning stages; complex requests add an Oracle
    contrarian-review stage.

- Simple tasks skip research, go straight to Builder/Operations
- Moderate tasks follow the standard pipeline
- Complex tasks add Oracle contrarian review stage
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from vetinari.types import AgentType

logger = logging.getLogger(__name__)


# Cyclomatic complexity thresholds for the AST-based classifier
_AST_COMPLEX_THRESHOLD = 10  # McCabe score above this → COMPLEX
_AST_SIMPLE_THRESHOLD = 3  # McCabe score below this → SIMPLE


class Complexity(Enum):
    """Task complexity classification for routing decisions."""

    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"


@dataclass
class RoutingDecision:
    """Result of complexity routing analysis."""

    complexity: Complexity
    skip_stages: list[str]
    add_stages: list[str]
    recommended_agents: list[str]
    reasoning: str
    execution_strategy: str = "pipeline"  # "pipeline" or "code_mode"
    strategy_bundle: dict[str, Any] | None = None  # MetaAdapter recommendations

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"RoutingDecision(complexity={self.complexity!r}, execution_strategy={self.execution_strategy!r})"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary.

        Returns:
            Dictionary representation of the routing decision.
        """
        result = {
            "complexity": self.complexity.value,
            "skip_stages": self.skip_stages,
            "add_stages": self.add_stages,
            "recommended_agents": self.recommended_agents,
            "reasoning": self.reasoning,
            "execution_strategy": self.execution_strategy,
        }
        if self.strategy_bundle:
            result["strategy_bundle"] = self.strategy_bundle
        return result


# ── Complexity signals ────────────────────────────────────────────────

_COMPLEX_SIGNALS = [
    r"architect",
    r"security audit",
    r"refactor\s+(?:entire|all|full|whole)",
    r"migrat(?:e|ion)",
    r"multi[- ]?(?:service|module|component)",
    r"distributed",
    r"backwards?\s*compat",
    r"performance\s+(?:critical|sensitive)",
    r"risk\s+assess",
    r"compliance",
    r"design\s+system",
]

_SIMPLE_SIGNALS = [
    r"^(?:fix|update|change|rename|add|remove)\s+\w+$",
    r"typo",
    r"bump\s+version",
    r"update\s+(?:readme|docs|changelog)",
    r"^format\b",
    r"^lint\b",
    r"simple\s+(?:bug|fix|change)",
    r"one[- ]line",
    r"trivial",
]


def _infer_task_type(description: str) -> str:
    """Infer task type from description for MetaAdapter consultation.

    Args:
        description: Task description text.

    Returns:
        Task type string (coding, research, docs, devops, general).
    """
    desc_lower = description.lower()
    if any(kw in desc_lower for kw in ("code", "implement", "build", "fix", "refactor")):
        return "coding"
    if any(kw in desc_lower for kw in ("research", "analyze", "investigate")):
        return "research"
    if any(kw in desc_lower for kw in ("document", "readme", "docs")):
        return "docs"
    if any(kw in desc_lower for kw in ("deploy", "docker", "ci/cd", "kubernetes")):
        return "devops"
    return "general"


def _ast_complexity_from_description(description: str) -> dict[str, Any] | None:
    """Estimate task complexity from embedded code or prose."""
    if not description or not description.strip():
        return None
    import ast as _ast

    code_block_re = re.compile(r"```(?:python)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
    snippets = code_block_re.findall(description)
    if not snippets and re.search(r"\bdef\s+\w+\s*\(", description):
        snippets = [description]
    cyclomatic = 1  # baseline
    for snippet in snippets:
        try:
            tree = _ast.parse(snippet)
        except SyntaxError:
            logger.warning("Could not parse code snippet for complexity analysis — skipping snippet")
            continue
        for node in _ast.walk(tree):
            if isinstance(
                node,
                (
                    _ast.If,
                    _ast.For,
                    _ast.While,
                    _ast.ExceptHandler,
                    _ast.With,
                    _ast.Assert,
                    _ast.comprehension,
                    _ast.BoolOp,
                ),
            ):
                cyclomatic += 1
    desc_lower = description.lower()
    complex_score = sum(1 for p in _COMPLEX_SIGNALS if re.search(p, desc_lower))
    simple_score = sum(1 for p in _SIMPLE_SIGNALS if re.search(p, desc_lower))
    word_count = len(description.split())
    cyclomatic += complex_score * 2
    if cyclomatic >= _AST_COMPLEX_THRESHOLD or (complex_score >= 3 and simple_score == 0):
        classification = "COMPLEX"
        risk = "HIGH"
    elif cyclomatic <= _AST_SIMPLE_THRESHOLD and simple_score >= 1:
        classification = "SIMPLE"
        risk = "LOW"
    else:
        classification = "MODERATE"
        risk = "MEDIUM"
    estimated_files = max(1, word_count // 30)
    logger.debug(
        "_ast_complexity_from_description: cyclomatic=%d classification=%s risk=%s",
        cyclomatic,
        classification,
        risk,
    )
    return {
        "classification": classification,
        "cyclomatic": cyclomatic,
        "risk": risk,
        "files": estimated_files,
    }


def classify_complexity(
    description: str,
    task_count: int = 1,
    estimated_files: int = 0,
) -> Complexity:
    """Classify a task's complexity.

    Uses keyword analysis, task count, and estimated file count as signals.

    Args:
        description: The description.
        task_count: The task count.
        estimated_files: The estimated files.

    Returns:
        The Complexity result.
    """
    desc_lower = description.lower()

    # Check for explicit complexity signals
    complex_score = sum(1 for p in _COMPLEX_SIGNALS if re.search(p, desc_lower))
    simple_score = sum(1 for p in _SIMPLE_SIGNALS if re.search(p, desc_lower))

    # Word count as proxy for specification complexity
    word_count = len(description.split())
    if word_count > 100:
        complex_score += 2
    elif word_count > 50:
        complex_score += 1
    elif word_count < 15:
        simple_score += 1

    # Task count and file estimates
    if task_count > 5:
        complex_score += 2
    elif task_count > 2:
        complex_score += 1

    if estimated_files > 10:
        complex_score += 1
    elif estimated_files <= 2:
        simple_score += 1

    # ── Borderline detection: if scores are close, consult LLM ────────
    score_diff = abs(complex_score - simple_score)
    is_borderline = score_diff <= 1 and complex_score > 0 and simple_score > 0

    if is_borderline:
        ast_result = _ast_complexity_from_description(description)
        if ast_result and "classification" in ast_result:
            ast_class = ast_result["classification"]
            logger.info(
                "AST complexity assessment for borderline case: %s (files=%s, risk=%s, cyclomatic=%s)",
                ast_class,
                ast_result.get("files", "?"),
                ast_result.get("risk", "?"),
                ast_result.get("cyclomatic", "?"),
            )
            complexity_map = {
                "SIMPLE": Complexity.SIMPLE,
                "MODERATE": Complexity.MODERATE,
                "COMPLEX": Complexity.COMPLEX,
            }
            if ast_class in complexity_map:
                return complexity_map[ast_class]

    # Decision (heuristic fallback)
    if complex_score >= 3 and complex_score > simple_score:
        return Complexity.COMPLEX
    if simple_score >= 2 and simple_score > complex_score:
        return Complexity.SIMPLE
    return Complexity.MODERATE


def route_by_complexity(
    description: str,
    task_count: int = 1,
    estimated_files: int = 0,
) -> RoutingDecision:
    """Produce a routing decision based on task complexity.

    Returns which pipeline stages to skip/add and which agents to engage.
    Also consults the MetaAdapter for strategy parameter recommendations
    based on past episode similarity.

    Args:
        description: The description.
        task_count: The task count.
        estimated_files: The estimated files.

    Returns:
        The RoutingDecision result.
    """
    complexity = classify_complexity(description, task_count, estimated_files)

    # Consult MetaAdapter for strategy recommendations based on past episodes
    strategy_bundle = None
    try:
        from vetinari.learning.meta_adapter import get_meta_adapter

        adapter = get_meta_adapter()
        strategy_bundle = adapter.select_strategy(
            task_description=description,
            task_type=_infer_task_type(description),
        )
        logger.debug(
            "MetaAdapter strategy for '%s': %s (source=%s)",
            description[:60],
            strategy_bundle.decomposition_granularity,
            strategy_bundle.source,
        )
    except Exception:
        logger.warning("MetaAdapter unavailable for complexity routing")

    # Convert strategy bundle to dict for inclusion in routing decision
    _strategy_dict = strategy_bundle.to_dict() if strategy_bundle else None

    if complexity == Complexity.SIMPLE:
        return RoutingDecision(
            complexity=complexity,
            skip_stages=["research", "contrarian_review"],
            add_stages=[],
            recommended_agents=[AgentType.WORKER.value],
            reasoning="Simple task — skip research, direct to execution",
            execution_strategy="pipeline",
            strategy_bundle=_strategy_dict,
        )
    if complexity == Complexity.COMPLEX:
        return RoutingDecision(
            complexity=complexity,
            skip_stages=[],
            add_stages=["contrarian_review", "risk_assessment"],
            recommended_agents=[AgentType.FOREMAN.value, AgentType.WORKER.value, AgentType.INSPECTOR.value],
            reasoning="Complex task — full pipeline with Inspector contrarian review",
            execution_strategy="code_mode" if task_count > 3 else "pipeline",
            strategy_bundle=_strategy_dict,
        )
    return RoutingDecision(
        complexity=complexity,
        skip_stages=[],
        add_stages=[],
        recommended_agents=[AgentType.FOREMAN.value, AgentType.WORKER.value, AgentType.INSPECTOR.value],
        reasoning="Moderate task — standard pipeline",
        execution_strategy="pipeline",
        strategy_bundle=_strategy_dict,
    )
