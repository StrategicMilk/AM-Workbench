"""Root Cause Analysis module for classifying quality rejections."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum

from vetinari.validation.root_cause_graph import (
    CausalEdge,
    CausalGraph,
    build_causal_graph,
    walk_graph_for_root_cause,
)

logger = logging.getLogger(__name__)


class DefectCategory(Enum):
    """Classification of why a quality rejection occurred."""

    BAD_SPEC = "bad_spec"
    WRONG_MODEL = "wrong_model"
    INSUFFICIENT_CONTEXT = "context"
    PROMPT_WEAKNESS = "prompt"
    COMPLEXITY_UNDERESTIMATE = "complexity"
    INTEGRATION_ERROR = "integration"
    HALLUCINATION = "hallucination"


@dataclass
class RootCauseAnalysis:
    """Result of a root cause analysis on a rejected task output."""

    category: DefectCategory
    confidence: float
    evidence: list[str]
    corrective_action: str
    preventive_action: str

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"RootCauseAnalysis(category={self.category!r}, confidence={self.confidence!r})"


class RootCauseAnalyzer:
    """Classifies the root cause of a quality rejection."""

    def analyze(
        self,
        task_description: str,
        rejection_reasons: list[str],
        quality_score: float,
        task_mode: str = "",
    ) -> RootCauseAnalysis:
        """Classify the root cause of a quality rejection.

        Args:
            task_description: Task description value consumed by analyze().
            rejection_reasons: Rejection reasons value consumed by analyze().
            quality_score: Score value evaluated by the operation.
            task_mode: Task mode value consumed by analyze().

        Returns:
            Value produced for the caller.
        """
        llm_analysis = self._try_llm_analysis(task_description, rejection_reasons, task_mode)
        if llm_analysis:
            return llm_analysis

        if self._is_hallucination(rejection_reasons):
            return self._classification_for_signal(
                DefectCategory.HALLUCINATION,
                0.90,
                rejection_reasons,
                ("import", "not found", "does not exist", "hallucinated", "fabricated"),
                "Regenerate the output grounding all references in verified sources. "
                "Remove any imports, functions, or facts that cannot be confirmed.",
                "Add a verification pass that checks all referenced symbols and imports exist "
                "before accepting output from the agent.",
            )
        if self._is_bad_spec(rejection_reasons):
            return self._classification_for_signal(
                DefectCategory.BAD_SPEC,
                0.85,
                rejection_reasons,
                ("ambiguous", "unclear", "acceptance criteria", "spec", "incomplete"),
                "Clarify the task specification with the requester before retrying. "
                "Resolve all ambiguous requirements and add explicit acceptance criteria.",
                "Introduce a spec review step in the planning phase that enforces "
                "acceptance criteria and unambiguous requirements before agent dispatch.",
            )
        if self._is_insufficient_context(rejection_reasons):
            return self._classification_for_signal(
                DefectCategory.INSUFFICIENT_CONTEXT,
                0.82,
                rejection_reasons,
                ("context", "missing information", "not provided"),
                "Gather the missing context or documentation and retry with an enriched prompt. "
                "Include relevant background, prior decisions, and data sources.",
                "Expand the context-retrieval step in the pipeline to proactively fetch "
                "related artifacts, prior outputs, and domain references before agent execution.",
            )
        if self._is_integration_error(rejection_reasons):
            return self._classification_for_signal(
                DefectCategory.INTEGRATION_ERROR,
                0.85,
                rejection_reasons,
                ("integration", "breaks", "conflict", "incompatible"),
                "Run integration tests to identify the failing interface. "
                "Align the output with the contract expected by downstream components.",
                "Add integration smoke tests to the quality gate so integration failures are caught before review.",
            )
        if self._is_complexity_issue(rejection_reasons):
            return self._classification_for_signal(
                DefectCategory.COMPLEXITY_UNDERESTIMATE,
                0.78,
                rejection_reasons,
                ("complex", "too large", "split", "decompose"),
                "Decompose the task into smaller, independently verifiable subtasks. "
                "Re-plan with granular steps before retrying.",
                "Improve complexity estimation in the planning phase. "
                "Set a maximum task size threshold and enforce decomposition above it.",
            )
        if self._has_wrong_model_evidence(rejection_reasons):
            return self._wrong_model_analysis(rejection_reasons, quality_score)
        return self._prompt_weakness_fallback(rejection_reasons)

    @staticmethod
    def _try_llm_analysis(
        task_description: str,
        rejection_reasons: list[str],
        task_mode: str,
    ) -> RootCauseAnalysis | None:
        try:
            from vetinari.llm_helpers import diagnose_defect_via_llm

            llm_result = diagnose_defect_via_llm(
                task_description=task_description,
                rejection_reason="; ".join(rejection_reasons[:3]),
                agent_type=task_mode,
            )
        except Exception:
            logger.warning("LLM root cause analysis unavailable; falling back to heuristic root cause detection")
            return None
        if not llm_result:
            return None
        llm_category, llm_explanation = llm_result
        category_map = {
            "hallucinated_import": DefectCategory.HALLUCINATION,
            "ambiguous_spec": DefectCategory.BAD_SPEC,
            "model_limitation": DefectCategory.WRONG_MODEL,
            "insufficient_context": DefectCategory.INSUFFICIENT_CONTEXT,
            "output_format": DefectCategory.PROMPT_WEAKNESS,
            "runtime_error": DefectCategory.INTEGRATION_ERROR,
            "quality_below_threshold": DefectCategory.PROMPT_WEAKNESS,
        }
        mapped = category_map.get(llm_category)
        if not mapped:
            return None
        logger.info("LLM diagnosed root cause as %s: %s", mapped.value, llm_explanation)
        return RootCauseAnalysis(
            category=mapped,
            confidence=0.85,
            evidence=[llm_explanation, *rejection_reasons[:2]],
            corrective_action=llm_explanation,
            preventive_action=f"Address underlying {mapped.value} pattern: {llm_explanation}",
        )

    @staticmethod
    def _classification_for_signal(
        category: DefectCategory,
        confidence: float,
        rejection_reasons: list[str],
        evidence_keywords: tuple[str, ...],
        corrective_action: str,
        preventive_action: str,
    ) -> RootCauseAnalysis:
        evidence = [reason for reason in rejection_reasons if any(kw in reason.lower() for kw in evidence_keywords)]
        logger.info("Root cause classified as %s (evidence count=%d)", category.name, len(evidence))
        return RootCauseAnalysis(
            category=category,
            confidence=confidence,
            evidence=evidence or rejection_reasons[:1],
            corrective_action=corrective_action,
            preventive_action=preventive_action,
        )

    @staticmethod
    def _wrong_model_analysis(rejection_reasons: list[str], quality_score: float) -> RootCauseAnalysis:
        evidence = [
            reason for reason in rejection_reasons if any(kw in reason.lower() for kw in ("capability", "model"))
        ]
        if quality_score < 0.3:
            evidence = evidence or [f"Quality score {quality_score:.2f} is below capability threshold 0.30"]
        logger.info(
            "Root cause classified as WRONG_MODEL (score=%.2f, evidence count=%d)", quality_score, len(evidence)
        )
        return RootCauseAnalysis(
            category=DefectCategory.WRONG_MODEL,
            confidence=0.80,
            evidence=evidence or rejection_reasons[:1],
            corrective_action=(
                "Re-route the task to a more capable model or a specialist agent. "
                "Consider decomposing the task into subtasks within model capabilities."
            ),
            preventive_action="Update the model routing rules to match task capability requirements against profiles.",
        )

    @staticmethod
    def _prompt_weakness_fallback(rejection_reasons: list[str]) -> RootCauseAnalysis:
        logger.info(
            "Root cause classified as PROMPT_WEAKNESS (fallback, no strong signal in %d reasons)",
            len(rejection_reasons),
        )
        return RootCauseAnalysis(
            category=DefectCategory.PROMPT_WEAKNESS,
            confidence=0.50,
            evidence=rejection_reasons[:2] if rejection_reasons else ["No specific signal detected"],
            corrective_action="Revise the prompt to be explicit about output format, constraints, and success criteria.",
            preventive_action="Review and refine prompt templates for this task type. Add few-shot examples.",
        )

    @staticmethod
    def _is_hallucination(reasons: list[str]) -> bool:
        """Return True if any reason signals a hallucination defect."""
        keywords = ("import", "not found", "does not exist", "hallucinated", "fabricated")
        return any(kw in " ".join(reasons).lower() for kw in keywords)

    @staticmethod
    def _is_bad_spec(reasons: list[str]) -> bool:
        """Return True if any reason signals an ambiguous or incomplete specification."""
        keywords = ("ambiguous", "unclear", "acceptance criteria", "spec", "incomplete", "bad_spec")
        combined = " ".join(reasons).lower()
        return any(re.search(r"\b" + re.escape(kw) + r"\b", combined) for kw in keywords)

    @staticmethod
    def _is_wrong_model(reasons: list[str], score: float) -> bool:
        """Return True if the model lacked capability for this task."""
        combined = " ".join(reasons).lower()
        keyword_hit = any(kw in combined for kw in ("capability", "model"))
        return keyword_hit or score < 0.3

    @staticmethod
    def _has_wrong_model_evidence(reasons: list[str]) -> bool:
        combined = " ".join(reasons).lower()
        return any(kw in combined for kw in ("capability", "model"))

    @staticmethod
    def _is_insufficient_context(reasons: list[str]) -> bool:
        """Return True if the agent lacked sufficient context to complete the task."""
        combined = " ".join(reasons).lower()
        return any(kw in combined for kw in ("context", "missing information", "not provided"))

    @staticmethod
    def _is_integration_error(reasons: list[str]) -> bool:
        """Return True if the output works in isolation but breaks integration."""
        combined = " ".join(reasons).lower()
        return any(kw in combined for kw in ("integration", "breaks", "conflict", "incompatible"))

    @staticmethod
    def _is_complexity_issue(reasons: list[str]) -> bool:
        """Return True if the task was harder or larger than originally estimated."""
        combined = " ".join(reasons).lower()
        return any(kw in combined for kw in ("complex", "too large", "split", "decompose"))


__all__ = [
    "CausalEdge",
    "CausalGraph",
    "DefectCategory",
    "RootCauseAnalysis",
    "RootCauseAnalyzer",
    "build_causal_graph",
    "walk_graph_for_root_cause",
]
