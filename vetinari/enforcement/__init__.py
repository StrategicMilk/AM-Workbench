"""Vetinari runtime constraint enforcement package.

Provides four enforcers that validate agent operations against the constraints
declared in each agent's ``AgentSpec``:

- ``DelegationDepthValidator``  — caps delegation nesting via ``max_delegation_depth``
- ``QualityGateEnforcer``       — enforces minimum quality scores via ``quality_gate_score``
- ``FileJurisdictionEnforcer``  — restricts file access via ``jurisdiction``
- ``AgentCapabilityEnforcer``   — checks capability availability via ``capabilities``

The ``enforce_all()`` convenience function runs all four checks in a single call
and is the recommended entry point for orchestration layers that want broad
coverage without importing each enforcer individually.

Violation aggregation: ``enforce_all()`` evaluates **every** applicable check,
collects all failures, and raises after all checks complete.  A single failure
re-raises as its original exception type; multiple failures are wrapped in
``CompositeEnforcementError`` so the caller sees the full set of violations.

Usage::

    from vetinari.enforcement import enforce_all

    # Raises after collecting all violations; returns None if all pass.
    enforce_all(
        agent_type=AgentType.WORKER,
        current_depth=1,
        quality_score=0.85,
        file_path="vetinari/agents/builder.py",
        required_capability="code_scaffolding",
    )
"""

from __future__ import annotations

import logging
from typing import Any

from vetinari.enforcement.capabilities import AgentCapabilityEnforcer
from vetinari.enforcement.depth import DelegationDepthValidator
from vetinari.enforcement.jurisdiction import FileJurisdictionEnforcer
from vetinari.enforcement.quality import QualityGateEnforcer
from vetinari.envs import EnvTierUnavailable
from vetinari.exceptions import CompositeEnforcementError
from vetinari.security.fail_closed import (
    FailClosedError,
    PathTraversalError,
    SandboxUnavailableError,
    SchemaOpenError,
    UntrustedInputError,
    assert_closed_schema,
    confine_to_root,
    require_sandbox_or_raise,
    sanitize_untrusted_text,
)
from vetinari.types import AgentType

logger = logging.getLogger(__name__)


__all__ = [
    "AgentCapabilityEnforcer",
    "CompositeEnforcementError",
    "DelegationDepthValidator",
    "EnvTierUnavailable",
    "FailClosedError",
    "FileJurisdictionEnforcer",
    "PathTraversalError",
    "QualityGateEnforcer",
    "SandboxUnavailableError",
    "SchemaOpenError",
    "UntrustedInputError",
    "assert_closed_schema",
    "confine_to_root",
    "enforce_all",
    "require_sandbox_or_raise",
    "sanitize_untrusted_text",
]

# Module-level singleton instances — callers may use these directly or
# instantiate their own copies for testing.
_depth_validator = DelegationDepthValidator()
_quality_enforcer = QualityGateEnforcer()
_jurisdiction_enforcer = FileJurisdictionEnforcer()
_capability_enforcer = AgentCapabilityEnforcer()


def enforce_all(
    agent_type: AgentType,
    *,
    current_depth: int | None = None,
    quality_score: float | None = None,
    file_path: str | None = None,
    required_capability: str | None = None,
    **_kwargs: Any,
) -> None:
    """Run all applicable enforcement checks for a single agent operation.

    Each check is only performed when the corresponding argument is provided.
    Checks are run in the following order:

    1. Delegation depth (if ``current_depth`` is given)
    2. Quality gate   (if ``quality_score`` is given)
    3. File jurisdiction (if ``file_path`` is given)
    4. Capability     (if ``required_capability`` is given)

    All applicable checks are evaluated regardless of intermediate failures.
    Violations are collected and raised together after all checks complete —
    a single violation re-raises as its original exception type, while
    multiple violations are wrapped in ``CompositeEnforcementError``.

    Args:
        agent_type: The agent type to validate against.
        current_depth: Delegation depth for depth enforcement.  Skipped when
            ``None``.
        quality_score: Quality score for quality gate enforcement.  Skipped when
            ``None``.
        file_path: File path for jurisdiction enforcement.  Skipped when ``None``.
        required_capability: Capability name for capability enforcement.  Skipped
            when ``None``.
        **_kwargs: Accepted but ignored — allows callers to pass extra context
            without breaking the call signature.

    Raises:
        CompositeEnforcementError: When one or more checks fail. Contains all
            individual violations so callers see the full picture.
        DelegationDepthExceeded: When only the depth check fails (single violation).
        QualityGateFailed: When only the quality gate check fails (single violation).
        JurisdictionViolation: When only the jurisdiction check fails (single violation).
        CapabilityNotAvailable: When only the capability check fails (single violation).
    """
    violations: list[Exception] = []

    if current_depth is not None:
        try:
            _depth_validator.validate(agent_type, current_depth)
        except Exception as exc:
            violations.append(exc)

    if quality_score is not None:
        try:
            _quality_enforcer.validate(agent_type, quality_score)
        except Exception as exc:
            violations.append(exc)

    if file_path is not None:
        try:
            _jurisdiction_enforcer.validate(agent_type, file_path)
        except Exception as exc:
            violations.append(exc)

    if required_capability is not None:
        try:
            _capability_enforcer.validate(agent_type, required_capability)
        except Exception as exc:
            violations.append(exc)

    if len(violations) == 1:
        raise violations[0]
    if len(violations) > 1:
        raise CompositeEnforcementError(violations, agent_type=agent_type.value)
