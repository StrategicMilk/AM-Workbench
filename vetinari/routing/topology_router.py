"""Topology Router — route tasks to named execution topologies.

Extends the complexity router by assigning one of six named topologies to
a task based on DAG shape analysis.  Topologies map to AgentGraph execution
strategies.

Topologies:
  EXPRESS          — single-step, no orchestration overhead
  SEQUENTIAL       — linear chain; tasks run one after another
  PARALLEL         — independent tasks; all run concurrently
  HIERARCHICAL     — nested delegation; Foreman breaks into sub-plans
  SCATTER_GATHER   — fan-out to multiple workers then aggregate results
  DEBATE           — multi-agent deliberation for high-stakes decisions
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from vetinari.routing.complexity_router import Complexity
from vetinari.types import AgentType

logger = logging.getLogger(__name__)


class Topology(Enum):
    """Named execution topology for a task or plan.

    Each value maps to a distinct orchestration strategy in AgentGraph.
    """

    EXPRESS = "express"  # Single-step, no overhead
    SEQUENTIAL = "sequential"  # Linear task chain
    PARALLEL = "parallel"  # Independent concurrent tasks
    HIERARCHICAL = "hierarchical"  # Recursive Foreman delegation
    SCATTER_GATHER = "scatter_gather"  # Fan-out + aggregate
    DEBATE = "debate"  # Multi-agent deliberation


@dataclass
class TopologyDecision:
    """Result of topology routing analysis.

    Args:
        topology: The recommended execution topology.
        complexity: Estimated task complexity.
        skip_stages: Pipeline stages that can be skipped for this topology.
        add_stages: Additional stages required by this topology.
        recommended_agents: Agent type values recommended for execution.
        reasoning: Human-readable explanation of the routing choice.
        execution_strategy: Low-level strategy hint (``"pipeline"`` or ``"code_mode"``).
        strategy_bundle: Optional additional recommendations from MetaAdapter.
    """

    topology: Topology
    complexity: Complexity
    skip_stages: list[str]
    add_stages: list[str]
    recommended_agents: list[str]
    reasoning: str
    execution_strategy: str = "pipeline"  # "pipeline" or "code_mode"
    strategy_bundle: dict[str, Any] | None = None

    def __repr__(self) -> str:
        return (
            f"TopologyDecision(topology={self.topology.value!r}, "
            f"complexity={self.complexity.value!r}, "
            f"execution_strategy={self.execution_strategy!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dictionary.

        Returns:
            Dictionary representation of this TopologyDecision.
        """
        return {
            "topology": self.topology.value,
            "complexity": self.complexity.value,
            "skip_stages": self.skip_stages,
            "add_stages": self.add_stages,
            "recommended_agents": self.recommended_agents,
            "reasoning": self.reasoning,
            "execution_strategy": self.execution_strategy,
            "strategy_bundle": self.strategy_bundle,
        }


# -- Keyword-based topology signals -----------------------------------------

_COMPLEX_KEYWORDS = re.compile(
    r"\b(architect|design|system|platform|infrastructure|migration|"
    r"refactor|framework|overhaul|enterprise|distributed|scalab|"
    r"security audit|full.?stack|end.?to.?end)\b",
    re.IGNORECASE,
)

_DEBATE_KEYWORDS = re.compile(
    r"\b(security|auth|authentication|authorization|cryptograph|"
    r"comply|compliance|regulation|gdpr|hipaa|sox|"
    r"architect|design decision|trade.?off|strategy)\b",
    re.IGNORECASE,
)

_PARALLEL_KEYWORDS = re.compile(
    r"\b(batch|concurrent|parallel|multiple|several|each|all|"
    r"collection|list of|set of)\b",
    re.IGNORECASE,
)

_SCATTER_GATHER_KEYWORDS = re.compile(
    r"\b(scatter|gather|fan-out|broadcast|parallel fan)\b",
    re.IGNORECASE,
)

_SEQUENTIAL_KEYWORDS = re.compile(
    r"\b(step by step|pipeline|sequence|first.*then|phase|stage|"
    r"workflow|process|procedure)\b",
    re.IGNORECASE,
)


class TopologyRouter:
    """Route tasks to named execution topologies based on content analysis.

    Combines keyword heuristics with optional DAG shape data from
    ``dag_analyzer.analyze_dag()`` to select the most efficient topology.

    Usage::

        router = TopologyRouter()
        decision = router.route("Analyse security of auth module", complexity_hint="complex")
        # decision.topology == Topology.DEBATE
    """

    def route(
        self,
        task_description: str,
        context: dict[str, Any] | None = None,
        dag_shape: Any | None = None,
        complexity_hint: str = "",
    ) -> TopologyDecision:
        """Analyse a task description and return a topology recommendation.

        Args:
            task_description: The human-readable task or goal text.
            context: Optional context dict (project metadata, constraints).
            dag_shape: Optional DAGShape from dag_analyzer; when provided
                the structural analysis overrides keyword heuristics.
            complexity_hint: Optional explicit hint (``"simple"``, ``"moderate"``,
                ``"complex"``).

        Returns:
            TopologyDecision with the recommended topology and supporting data.
        """
        if context is None:
            context = {}
        text = task_description.lower()

        complexity = self._assess_complexity(task_description, complexity_hint)

        # DAG shape overrides keyword heuristics when available
        if dag_shape is not None:
            topology_str = _dag_shape_to_topology(dag_shape)
            topology = Topology(topology_str)
            reasoning = f"DAG shape analysis: {dag_shape!r}"
        else:
            topology, reasoning = self._keyword_topology(task_description, text, complexity)

        skip_stages, add_stages = self._stage_adjustments(topology)
        recommended = self._recommend_agents(topology)
        execution_strategy = "code_mode" if topology == Topology.EXPRESS else "pipeline"

        logger.debug(
            "[TopologyRouter] task=%r topology=%s complexity=%s",
            task_description[:60],
            topology.value,
            complexity.value,
        )

        return TopologyDecision(
            topology=topology,
            complexity=complexity,
            skip_stages=skip_stages,
            add_stages=add_stages,
            recommended_agents=recommended,
            reasoning=reasoning,
            execution_strategy=execution_strategy,
        )

    @staticmethod
    def _assess_complexity(text: str, hint: str) -> Complexity:
        """Estimate task complexity from text and optional hint.

        Args:
            text: Task description.
            hint: Optional explicit complexity hint string.

        Returns:
            Complexity enum value.
        """
        if hint == "simple":
            return Complexity.SIMPLE
        if hint == "complex":
            return Complexity.COMPLEX
        if hint == "moderate":
            return Complexity.MODERATE

        if _COMPLEX_KEYWORDS.search(text):
            return Complexity.COMPLEX
        word_count = len(text.split())
        if word_count > 30:
            return Complexity.MODERATE
        return Complexity.SIMPLE

    @staticmethod
    def _keyword_topology(
        original: str,
        lower: str | None = None,
        complexity: Complexity | None = None,
    ) -> Topology | tuple[Topology, str]:
        """Select a topology using keyword heuristics.

        Args:
            original: Original task description text.
            lower: Lowercased version of the text.
            complexity: The assessed complexity level.

        Returns:
            Topology for legacy one-argument calls, otherwise ``(Topology, reason)``.
        """
        single_arg_call = lower is None and complexity is None
        lower = original.lower() if lower is None else lower
        complexity = Complexity.MODERATE if complexity is None else complexity

        if _DEBATE_KEYWORDS.search(original) and complexity == Complexity.COMPLEX:
            result = (
                Topology.DEBATE,
                "Security/architecture decision with complex scope - debate protocol recommended",
            )
            return result[0] if single_arg_call else result
        if complexity == Complexity.SIMPLE:
            result = (Topology.EXPRESS, "Simple task - no orchestration overhead needed")
            return result[0] if single_arg_call else result
        if _SCATTER_GATHER_KEYWORDS.search(original):
            result = (Topology.SCATTER_GATHER, "Scatter/gather workload detected - fan-out and aggregation recommended")
            return result[0] if single_arg_call else result
        if _PARALLEL_KEYWORDS.search(original) and complexity != Complexity.COMPLEX:
            result = (Topology.PARALLEL, "Parallel workload pattern detected - concurrent execution optimal")
            return result[0] if single_arg_call else result
        if complexity == Complexity.COMPLEX:
            result = (Topology.HIERARCHICAL, "Complex task - hierarchical Foreman decomposition recommended")
            return result[0] if single_arg_call else result
        if _SEQUENTIAL_KEYWORDS.search(original):
            result = (Topology.SEQUENTIAL, "Sequential workflow pattern detected")
            return result[0] if single_arg_call else result
        result = (Topology.SEQUENTIAL, "Default: moderate task executed as sequential pipeline")
        return result[0] if single_arg_call else result

    @staticmethod
    def _stage_adjustments(topology: Topology) -> tuple[list[str], list[str]]:
        """Determine stage modifications for a given topology.

        Args:
            topology: The selected Topology.

        Returns:
            Tuple of (skip_stages, add_stages) lists.
        """
        if topology == Topology.EXPRESS:
            return (["research", "review", "synthesis"], [])
        if topology == Topology.DEBATE:
            return ([], ["debate_protocol", "consensus_review"])
        if topology == Topology.SCATTER_GATHER:
            return ([], ["scatter", "gather", "aggregate"])
        if topology == Topology.PARALLEL:
            return (["sequential_gates"], ["parallel_dispatch"])
        if topology == Topology.HIERARCHICAL:
            return ([], ["recursive_decomposition"])
        return ([], [])  # SEQUENTIAL: standard pipeline

    @staticmethod
    def _recommend_agents(topology: Topology) -> list[str]:
        """Return recommended agent type values for a topology.

        Args:
            topology: The selected Topology.

        Returns:
            List of AgentType value strings.
        """
        foreman = AgentType.FOREMAN.value
        worker = AgentType.WORKER.value
        inspector = AgentType.INSPECTOR.value

        topology_agents = {
            Topology.EXPRESS: [worker],
            Topology.SEQUENTIAL: [foreman, worker, inspector],
            Topology.PARALLEL: [foreman, worker, worker, inspector],
            Topology.HIERARCHICAL: [foreman, foreman, worker, inspector],
            Topology.SCATTER_GATHER: [foreman, worker, worker, worker, inspector],
            Topology.DEBATE: [foreman, worker, inspector],
        }
        return topology_agents.get(topology, [foreman, worker, inspector])


def _dag_shape_to_topology(dag_shape: Any) -> str:
    """Convert a DAGShape object to a topology string.

    Args:
        dag_shape: A DAGShape instance from dag_analyzer.

    Returns:
        Topology value string.
    """
    try:
        from vetinari.routing.dag_analyzer import suggest_topology

        return suggest_topology(dag_shape)
    except Exception as exc:
        logger.warning("[TopologyRouter] DAG shape conversion failed: %s", exc)
        return Topology.SEQUENTIAL.value
