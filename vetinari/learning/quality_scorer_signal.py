"""OutcomeSignal wrapper helpers for QualityScorer."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from vetinari.agents.contracts import LLMJudgment, OutcomeSignal, Provenance
from vetinari.types import EvidenceBasis


class QualityScorerSignalMixin:
    """Mixin for converting QualityScore values into OutcomeSignal values."""

    if TYPE_CHECKING:
        score: Any

    def score_with_signal(
        self,
        task_id: str,
        model_id: str,
        task_type: str,
        task_description: str,
        output: str,
        use_llm: bool = True,
        inference_confidence: float | None = None,
        temperature_used: float | None = None,
    ) -> OutcomeSignal:
        """Score a task output and return an evidence-backed OutcomeSignal.

        Wraps ``score()`` with judgment/provenance metadata. Rejected outputs
        return ``passed=False`` with ``basis=UNSUPPORTED``.

        Args:
            task_id: Unique task identifier.
            model_id: Model that produced the output.
            task_type: Type of task (coding, research, etc.).
            task_description: What the task asked for.
            output: The output to evaluate.
            use_llm: Whether to attempt LLM-as-judge evaluation.
            inference_confidence: Optional confidence from logprob variance.
            temperature_used: The actual temperature used during inference.

        Returns:
            Evidence-backed OutcomeSignal for the scored output.
        """
        qs = self.score(
            task_id=task_id,
            model_id=model_id,
            task_type=task_type,
            task_description=task_description,
            output=output,
            use_llm=use_llm,
            inference_confidence=inference_confidence,
            temperature_used=temperature_used,
        )

        timestamp = datetime.now(timezone.utc).isoformat()

        if qs.method == "rejected":
            return OutcomeSignal(
                passed=False,
                score=0.0,
                basis=EvidenceBasis.UNSUPPORTED,
                issues=tuple(qs.issues),
                provenance=Provenance(
                    source="vetinari.learning.quality_scorer",
                    timestamp_utc=timestamp,
                    model_id=model_id,
                ),
            )

        judgment = LLMJudgment(
            model_id=qs.model_id,
            summary=f"Quality score {qs.overall_score:.3f} via {qs.method} for task_type={qs.task_type}",
            score=qs.overall_score,
            reasoning="; ".join(qs.issues) if qs.issues else "",
        )

        passed = qs.overall_score >= 0.5 and not qs.issues
        return OutcomeSignal(
            passed=passed,
            score=qs.overall_score,
            basis=EvidenceBasis.LLM_JUDGMENT,
            llm_judgment=judgment,
            issues=tuple(qs.issues),
            provenance=Provenance(
                source="vetinari.learning.quality_scorer",
                timestamp_utc=timestamp,
                model_id=qs.model_id,
            ),
        )
