"""Pre-execution prevention quality gate check implementation."""

from __future__ import annotations

from typing import Any

from vetinari.validation.gate_checks_helpers import _GateCheckHelpers
from vetinari.validation.gate_types import GateCheckResult, GateResult, QualityGateConfig


class _PreventionGateChecks(_GateCheckHelpers):
    """Prevention gate check implementation for runner mixins."""

    def check_prevention(self, artifacts: dict[str, Any], config: QualityGateConfig) -> GateCheckResult:
        """Run pre-execution prevention checks (poka-yoke).

        Validates task inputs before Builder executes using PreventionGate logic.
        Checks: acceptance criteria present, referenced files exist,
        context completeness, model capability, token budget, concurrent conflicts.

        Args:
            artifacts: Task input artifacts. Expected keys:
                ``task_description``, ``acceptance_criteria``, ``referenced_files``,
                ``model_capabilities``, ``required_capabilities``,
                ``estimated_tokens``, ``token_budget``, ``active_file_scopes``.
            config: The quality gate configuration for this check.

        Returns:
            GateCheckResult reflecting the prevention gate outcome.
        """
        from vetinari.validation.prevention import PreventionGate

        task_description: str = artifacts.get("task_description", "")
        acceptance_criteria: list[str] = artifacts.get("acceptance_criteria", [])
        referenced_files: list[str] = artifacts.get("referenced_files", [])
        model_capabilities: set[str] = artifacts.get("model_capabilities", set())
        required_capabilities: set[str] = artifacts.get("required_capabilities", set())
        estimated_tokens: int = artifacts.get("estimated_tokens", 0)
        token_budget: int = artifacts.get("token_budget", 100_000)
        active_file_scopes: set[str] = artifacts.get("active_file_scopes", set())

        gate = PreventionGate()
        prevention_result = gate.validate(
            task_description=task_description,
            acceptance_criteria=acceptance_criteria,
            referenced_files=referenced_files,
            model_capabilities=model_capabilities,
            required_capabilities=required_capabilities,
            estimated_tokens=estimated_tokens,
            token_budget=token_budget,
            active_file_scopes=active_file_scopes,
        )

        issues: list[dict[str, Any]] = []
        if prevention_result.passed:
            score = 1.0
            result_enum = self._score_to_result(score, config.min_score)
        else:
            score = 0.0
            result_enum = GateResult.FAILED
            issues.extend(
                {"severity": "error", "category": "prevention", "message": failure.reason}
                for failure in prevention_result.failures
            )

        return GateCheckResult(
            gate_name=config.name,
            mode=config.mode,
            result=result_enum,
            score=round(score, 3),
            issues=issues,
            suggestions=(
                [f"Recommended action: {prevention_result.recommendation}"] if not prevention_result.passed else []
            ),
            metadata={"recommendation": prevention_result.recommendation},
        )
