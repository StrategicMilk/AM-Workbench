"""Workflow Learner - Vetinari Self-Improvement Subsystem.

Mines execution history to learn which decomposition strategies work
best for different goal types. Uses a simple decision tree approach
(no external ML dependencies).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vetinari.boundary_guards import account_evidence_drop
from vetinari.constants import VETINARI_STATE_DIR
from vetinari.types import AgentType

logger = logging.getLogger(__name__)


@dataclass
class WorkflowPattern:
    """A learned workflow pattern mapping goal characteristics to outcomes."""

    pattern_id: str
    domain: str  # "coding" | "research" | "data" | "docs" | "general"
    avg_depth: float  # Optimal decomposition depth
    avg_breadth: float  # Optimal number of parallel tasks
    preferred_agents: list[str] = field(default_factory=list)
    success_rate: float = 0.7
    avg_quality: float = 0.7
    sample_count: int = 0
    last_updated: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"WorkflowPattern(pattern_id={self.pattern_id!r}, domain={self.domain!r},"
            f" success_rate={self.success_rate!r}, sample_count={self.sample_count!r})"
        )


class WorkflowLearner:
    """Learns effective workflow patterns from execution history.

    Analyzes completed plans to extract:
    - Optimal decomposition depth per domain
    - Best agent sequences per domain
    - Parallelism strategies that work

    Provides recommendations to ForemanAgent and TwoLayerOrchestrator.
    """

    def __init__(self):
        self._patterns: dict[str, WorkflowPattern] = {}
        self._lock = threading.Lock()
        self._load_patterns()

    def infer_domain(self, goal: str) -> str:
        """Classify goal into a domain category.

        Uses keyword matching on the lowercased goal text to assign one of
        five domains: coding, research, data, docs, or general.

        Args:
            goal: The goal or task description to classify.

        Returns:
            One of ``"coding"``, ``"research"``, ``"data"``, ``"docs"``, or
            ``"general"``.
        """
        g = goal.lower()
        if any(k in g for k in ["code", "implement", "build", "program", "app", "software"]):
            return "coding"
        if any(k in g for k in ["research", "analyze", "investigate", "study"]):
            return "research"
        if any(k in g for k in ["data", "database", "sql", "pipeline", "etl"]):
            return "data"
        if any(k in g for k in ["document", "readme", "write", "report", "explain"]):
            return "docs"
        return "general"

    def get_recommendations(self, goal: str) -> dict[str, Any]:
        """Return workflow recommendations for a goal.

        Returns:
            Dict with keys: domain, recommended_depth, recommended_breadth,
                           preferred_agents, confidence.
        """
        domain = self.infer_domain(goal)
        with self._lock:
            pattern = self._patterns.get(domain)

            if pattern and pattern.sample_count >= 3:
                return {
                    "domain": domain,
                    "recommended_depth": round(pattern.avg_depth),
                    "recommended_breadth": round(pattern.avg_breadth),
                    "preferred_agents": pattern.preferred_agents[:5],
                    "confidence": min(1.0, pattern.sample_count / 20),
                }

        # Default recommendations per domain
        defaults = {
            "coding": {"depth": 4, "breadth": 3, "agents": [AgentType.WORKER.value, AgentType.INSPECTOR.value]},
            "research": {"depth": 3, "breadth": 2, "agents": [AgentType.WORKER.value]},
            "data": {"depth": 4, "breadth": 2, "agents": [AgentType.WORKER.value, AgentType.INSPECTOR.value]},
            "docs": {"depth": 2, "breadth": 1, "agents": [AgentType.WORKER.value]},
            "general": {"depth": 3, "breadth": 2, "agents": [AgentType.WORKER.value, AgentType.INSPECTOR.value]},
        }
        d = defaults.get(domain, defaults["general"])
        return {
            "domain": domain,
            "recommended_depth": d["depth"],
            "recommended_breadth": d["breadth"],
            "preferred_agents": d["agents"],
            "confidence": 0.3,  # Low confidence -- using defaults
        }

    def record_outcome(
        self,
        goal: str,
        plan_depth: int,
        plan_breadth: int,
        agents_used: list[str],
        quality_score: float,
        success: bool,
    ) -> None:
        """Record a plan execution outcome to update patterns.

        Args:
            goal: The original goal.
            plan_depth: Maximum depth of the executed plan.
            plan_breadth: Maximum breadth (parallel tasks).
            agents_used: List of agent type strings used.
            quality_score: Overall quality score 0.0-1.0.
            success: Whether the plan succeeded.
        """
        domain = self.infer_domain(goal)
        self._update_pattern(domain, plan_depth, plan_breadth, agents_used, quality_score, success)

    def _update_pattern(
        self,
        domain: str,
        plan_depth: int,
        plan_breadth: int,
        agents_used: list[str],
        quality_score: float,
        success: bool,
    ) -> None:
        """Update the workflow pattern for a domain based on an observed outcome."""
        EMA = 0.2  # More smoothing — 0.3 was too reactive to single observations

        with self._lock:
            if domain in self._patterns:
                p = self._patterns[domain]
                p.avg_depth = (1 - EMA) * p.avg_depth + EMA * plan_depth
                p.avg_breadth = (1 - EMA) * p.avg_breadth + EMA * plan_breadth
                p.success_rate = (1 - EMA) * p.success_rate + EMA * float(success)
                p.avg_quality = (1 - EMA) * p.avg_quality + EMA * quality_score
                p.sample_count += 1
                # Update preferred agents (frequency-based)
                freq: dict[str, int] = {}
                for a in p.preferred_agents:
                    freq[a] = freq.get(a, 0) + 1
                for a in agents_used:
                    freq[a] = freq.get(a, 0) + 1
                p.preferred_agents = sorted(freq, key=freq.get, reverse=True)[:5]
                p.last_updated = datetime.now(timezone.utc).isoformat()
            else:
                self._patterns[domain] = WorkflowPattern(
                    pattern_id=f"pattern_{domain}",
                    domain=domain,
                    avg_depth=float(plan_depth),
                    avg_breadth=float(plan_breadth),
                    preferred_agents=agents_used[:5],
                    success_rate=float(success),
                    avg_quality=quality_score,
                    sample_count=1,
                )

            self._save_patterns()
        logger.debug("[WorkflowLearner] Updated pattern for domain '%s'", domain)

    def learn_from_benchmark(self, benchmark_result: dict[str, Any]) -> None:
        """Extract decomposition patterns from benchmark results.

        Analyses successful benchmark runs to learn which workflow structures
        (depth, breadth, agent sequences) correlate with high benchmark scores.
        Updates internal patterns so future plans benefit from benchmark insights.

        Args:
            benchmark_result: Dict with keys:
                - suite_name (str): benchmark suite identifier
                - task_type (str): task domain (coding, research, etc.)
                - pass_rate (float): 0.0-1.0 fraction of cases passed
                - avg_score (float): average benchmark score
                - total_cases (int): number of cases in the suite
                - passed_cases (int): number of cases that passed
                - results (list[dict]): optional per-case results with metadata
                - metadata (dict): optional extra metadata (agents_used, depth, breadth)
        """
        task_type = benchmark_result.get("task_type", "general")
        pass_rate = float(benchmark_result.get("pass_rate", 0.0))
        avg_score = float(benchmark_result.get("avg_score", pass_rate))
        suite_name = benchmark_result.get("suite_name", "unknown")
        metadata = benchmark_result.get("metadata", {})

        # Only learn from reasonably successful benchmark runs
        if pass_rate < 0.3:
            logger.debug(
                f"[WorkflowLearner] Skipping benchmark '{suite_name}' with "
                f"pass_rate={pass_rate:.2f} (too low to extract useful patterns)",
            )
            return

        domain = self._infer_domain_from_task_type(task_type)

        # Extract structural hints from metadata if available
        depth = metadata.get("depth", self._default_depth(domain))
        breadth = metadata.get("breadth", self._default_breadth(domain))
        agents_used = metadata.get("agents_used", self._default_agents(domain))

        # Scale learning rate by benchmark quality -- higher pass_rate = stronger signal
        quality_weight = pass_rate

        logger.debug(
            f"[WorkflowLearner] Learning from benchmark '{suite_name}': "
            f"domain={domain}, pass_rate={pass_rate:.2f}, avg_score={avg_score:.2f}",
        )

        # Update patterns directly using the correctly-inferred domain
        # (We don't go through record_outcome because its infer_domain uses
        # keyword matching on the goal string, which may not map benchmark
        # task_type strings correctly.)
        agents_list = agents_used if isinstance(agents_used, list) else [agents_used]
        self._update_pattern(
            domain=domain,
            plan_depth=int(depth),
            plan_breadth=int(breadth),
            agents_used=agents_list,
            quality_score=avg_score * quality_weight,
            success=pass_rate >= 0.5,
        )

        # Extract per-case patterns from detailed results if available
        per_case_results = benchmark_result.get("results", [])
        if per_case_results:
            self._extract_case_patterns(domain, per_case_results)

    def _extract_case_patterns(self, domain: str, results: list[dict[str, Any]]) -> None:
        """Extract patterns from individual benchmark case results.

        Looks at the highest-scoring cases to identify what made them succeed.
        """
        # Sort by score descending; learn from top performers
        sorted_results = sorted(
            results,
            key=lambda r: r.get("score", 0.0),
            reverse=True,
        )

        successful = [r for r in sorted_results if r.get("passed", False) or r.get("score", 0) >= 0.7]
        if not successful:
            return

        # Aggregate metadata from successful cases to find common patterns
        agent_freq: dict[str, int] = {}
        for result in successful[:10]:  # Top 10 successful cases
            case_meta = result.get("metadata", {})
            for agent in case_meta.get("agents_used", []):
                agent_freq[agent] = agent_freq.get(agent, 0) + 1

        if agent_freq and domain in self._patterns:
            # Merge successful benchmark agents into preferred_agents
            pattern = self._patterns[domain]
            existing_freq: dict[str, int] = dict.fromkeys(pattern.preferred_agents, 1)
            for agent, count in agent_freq.items():
                existing_freq[agent] = existing_freq.get(agent, 0) + count
            pattern.preferred_agents = sorted(existing_freq, key=existing_freq.get, reverse=True)[:5]
            self._save_patterns()

    def _infer_domain_from_task_type(self, task_type: str) -> str:
        """Map a benchmark task_type to our domain categories."""
        mapping = {
            "coding": "coding",
            "code": "coding",
            "programming": "coding",
            "research": "research",
            "analysis": "research",
            "data": "data",
            "database": "data",
            "documentation": "docs",
            "writing": "docs",
            "testing": "coding",
            "review": "coding",
            "planning": "general",
        }
        return mapping.get(task_type.lower(), self.infer_domain(task_type))

    @staticmethod
    def _default_depth(domain: str) -> int:
        defaults = {"coding": 4, "research": 3, "data": 4, "docs": 2, "general": 3}
        return defaults.get(domain, 3)

    @staticmethod
    def _default_breadth(domain: str) -> int:
        defaults = {"coding": 3, "research": 2, "data": 2, "docs": 1, "general": 2}
        return defaults.get(domain, 2)

    @staticmethod
    def _default_agents(domain: str) -> list[str]:
        defaults = {
            "coding": [AgentType.WORKER.value, AgentType.INSPECTOR.value],
            "research": [AgentType.WORKER.value],
            "data": [AgentType.WORKER.value, AgentType.INSPECTOR.value],
            "docs": [AgentType.WORKER.value],
            "general": [AgentType.WORKER.value, AgentType.INSPECTOR.value],
        }
        return defaults.get(domain, [AgentType.WORKER.value])

    def get_all_patterns(self) -> list[dict[str, Any]]:
        """Get all learned patterns.

        Returns:
            List of all WorkflowPattern instances serialized as plain dicts,
            one per domain that has been observed. Empty list if no outcomes
            have been recorded yet.
        """
        with self._lock:
            return [asdict(p) for p in self._patterns.values()]

    @staticmethod
    def _get_state_path() -> Path:
        """Resolve path for workflow pattern state file.

        Uses VETINARI_STATE_DIR env var if set, otherwise falls back to
        project-relative .vetinari/ directory.

        Returns:
            Path to workflow_patterns.json.
        """
        state_dir_env = os.environ.get("VETINARI_STATE_DIR", "")
        if state_dir_env:
            state_dir = Path(state_dir_env)
        else:
            state_dir = VETINARI_STATE_DIR
        state_dir.mkdir(parents=True, exist_ok=True)
        return state_dir / "workflow_patterns.json"

    def _load_patterns(self) -> None:
        try:
            path = self._get_state_path()
            if path.exists():
                with Path(path).open(encoding="utf-8") as f:
                    data = json.load(f)
                for item in data:
                    p = WorkflowPattern(**item)
                    self._patterns[p.domain] = p
                logger.debug("[WorkflowLearner] Loaded %s patterns", len(self._patterns))
        except Exception:
            account_evidence_drop("workflow_patterns_load", "workflow_learner", logger=logger)
            logger.error("Could not load workflow patterns", exc_info=True)
            raise

    def _save_patterns(self) -> None:
        try:
            path = self._get_state_path()
            with Path(path).open("w", encoding="utf-8") as f:
                json.dump([asdict(p) for p in self._patterns.values()], f, indent=2)
        except Exception:
            account_evidence_drop("workflow_patterns_save", "workflow_learner", logger=logger)
            logger.error("Could not save workflow patterns", exc_info=True)
            raise


_workflow_learner: WorkflowLearner | None = None
_workflow_learner_lock = threading.Lock()


def get_workflow_learner() -> WorkflowLearner:
    """Return the module-level WorkflowLearner singleton, creating it if needed.

    Returns:
        The shared WorkflowLearner instance, pre-loaded with any previously
        persisted workflow patterns from disk.
    """
    global _workflow_learner
    if _workflow_learner is None:
        with _workflow_learner_lock:
            if _workflow_learner is None:
                _workflow_learner = WorkflowLearner()
    return _workflow_learner
