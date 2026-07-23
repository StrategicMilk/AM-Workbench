"""Quality gate check runner import surface for QualityGateRunner.

The concrete gate check implementations live in the ``gate_checks_*`` helper
modules. ``QualityGateRunner`` in ``quality_gates.py`` inherits
``_GateCheckRunner`` from this module, preserving the existing import path while
keeping this coordination class below the module-size ceiling.
"""

from __future__ import annotations

from typing import Any

from vetinari.validation.gate_checks_prevention import _PreventionGateChecks
from vetinari.validation.gate_checks_standard import _StandardGateChecks
from vetinari.validation.gate_types import GateCheckResult, GateResult, QualityGateConfig, VerificationMode


class _GateCheckRunner(_StandardGateChecks, _PreventionGateChecks):
    """Gate check dispatch surface for ``QualityGateRunner``.

    This class is not meant to be instantiated directly; it preserves the
    existing inherited methods used by ``QualityGateRunner`` while keeping each
    implementation cluster in a focused helper module.
    """

    # ------------------------------------------------------------------
    # Internal dispatch used by QualityGateRunner
    # ------------------------------------------------------------------

    def _run_single_gate(self, config: QualityGateConfig, artifacts: dict[str, Any]) -> GateCheckResult:
        """Dispatch to the appropriate check method based on verification mode.

        Args:
            config: Gate configuration specifying the mode.
            artifacts: The artifacts to check.

        Returns:
            GateCheckResult from the dispatched check method.
        """
        dispatch = {
            VerificationMode.VERIFY_QUALITY: self.check_quality,
            VerificationMode.SECURITY: self.check_security,
            VerificationMode.VERIFY_COVERAGE: self.check_coverage,
            VerificationMode.VERIFY_ARCHITECTURE: self.check_architecture,
            VerificationMode.PRE_EXECUTION: self.check_prevention,
        }
        handler = dispatch.get(config.mode)
        if handler is None:
            return GateCheckResult(
                gate_name=config.name,
                mode=config.mode,
                result=GateResult.WARNING,
                score=0.5,
                issues=[
                    {
                        "severity": "warning",
                        "message": f"No handler for verification mode: {config.mode.value}",
                    },
                ],
            )
        return handler(artifacts, config)
