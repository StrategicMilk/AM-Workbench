"""Vetinari Exception Hierarchy.

=============================

Centralized exception definitions. Import from here rather than defining
ad-hoc exceptions throughout the codebase.

Usage::

    from vetinari.exceptions import InferenceError, ConfigurationError

    raise InferenceError("Model not responding", model_id="qwen-7b")
"""

from __future__ import annotations

from typing import Any


class VetinariError(Exception):
    """Base exception for all Vetinari errors."""

    def __init__(self, message: str = "", **context):
        self.context = context
        super().__init__(message)

    def __str__(self) -> str:
        base = super().__str__()
        if self.context:
            ctx = ", ".join(f"{k}={v!r}" for k, v in self.context.items())
            return f"{base} [{ctx}]"
        return base


# ---------------------------------------------------------------------------
# Infrastructure errors
# ---------------------------------------------------------------------------


class ConfigurationError(VetinariError):
    """Invalid or missing configuration."""


class StorageError(VetinariError):
    """Filesystem or persistence layer failure."""


class EngineError(VetinariError):
    """Base error for the owned AM Engine runtime."""


class EngineUnavailableError(EngineError):
    """The AM Engine cannot currently accept requests."""


class EngineBinaryMissingError(EngineError):
    """The pinned AM Engine binary is not installed."""


class EngineBinaryCorruptError(EngineError):
    """The AM Engine binary or release archive failed integrity verification."""


class EngineVersionMismatchError(EngineError):
    """The running AM Engine version does not match the pinned release."""


# ---------------------------------------------------------------------------
# Inference / adapter errors
# ---------------------------------------------------------------------------


class InferenceError(VetinariError):
    """LLM inference failed after all retries."""


class AdapterError(VetinariError):
    """Adapter-level communication failure."""


class ModelNotFoundError(InferenceError):
    """Requested model is not loaded or available."""


class ModelUnavailableError(InferenceError):
    """No model is available to serve inference requests."""


class VetinariTimeoutError(InferenceError):
    """Inference timed out.

    Named to avoid shadowing the built-in ``TimeoutError``.
    """


# Backward-compatible alias — prefer VetinariTimeoutError in new code.
InferenceTimeoutError = VetinariTimeoutError


# ---------------------------------------------------------------------------
# Agent / orchestration errors
# ---------------------------------------------------------------------------


class AgentError(VetinariError):
    """An agent failed to execute its task."""


class PlanningError(AgentError):
    """Plan generation or decomposition failed."""


class ExecutionError(AgentError):
    """Task execution failed."""


class ClarificationNeeded(VetinariError):
    """A task needs human clarification before execution can continue.

    Agents raise this when the next action is ambiguous and retrying would
    only repeat the same missing-context failure. The durable execution engine
    catches it, pauses the single task, persists the questions, and waits for
    ``resume_after_clarification`` to continue. Retrying is out of scope for
    this exception; only the clarification resume consumer should unpause it.

    Args:
        questions: Non-empty questions that must be answered.
        context: Task context available at the pause point.
        task_id: Optional task identifier.
        execution_id: Optional execution identifier.
    """

    def __init__(
        self,
        questions: list[str],
        context: dict[str, Any],
        task_id: str | None = None,
        execution_id: str | None = None,
    ) -> None:
        if not questions:
            raise ValueError("ClarificationNeeded requires at least one question")
        self.questions = questions
        self.context = context
        self.task_id = task_id
        self.execution_id = execution_id
        super().__init__(f"clarification needed: {len(questions)} question(s)")


class ExecutionNotFound(VetinariError):
    """Raised when a clarification resume target cannot be found."""

    def __init__(self, execution_id: str) -> None:
        self.execution_id = execution_id
        super().__init__(f"execution not found: {execution_id}")


class MissingCorrelationContext(RuntimeError):
    """Raised when required correlation IDs are absent before threaded dispatch."""


class VerificationError(AgentError):
    """Output verification failed quality checks."""


class CircularDependencyError(PlanningError):
    """Execution graph contains circular dependencies."""


# ---------------------------------------------------------------------------
# Security / safety errors
# ---------------------------------------------------------------------------


class SecurityError(VetinariError):
    """Security policy violation."""


class SandboxError(SecurityError):
    """Sandbox constraint violation."""


class SandboxPolicyViolation(SandboxError):
    """A sandboxed operation attempted an action the policy forbids.

    Distinct from SandboxError so callers can match specifically on policy
    violations (e.g., blocked path writes) without catching every sandbox
    constraint failure.
    """


class GuardrailError(SecurityError):
    """Input/output guardrail triggered."""


# ---------------------------------------------------------------------------
# Enforcement errors
# ---------------------------------------------------------------------------


class CapabilityNotAvailable(AgentError):
    """Agent attempted an action outside its declared capabilities."""


class DelegationDepthExceeded(AgentError):
    """Agent delegation chain exceeded maximum allowed depth."""


class JurisdictionViolation(AgentError):
    """Agent attempted to modify files outside its jurisdiction."""


class QualityGateFailed(AgentError):
    """Output did not meet required quality gate thresholds."""


class CompositeEnforcementError(AgentError):
    """Multiple enforcement violations detected in a single check.

    Collects all violations rather than failing on the first one, so callers
    can see the full list of issues.

    Args:
        violations: List of individual exception instances.
    """

    def __init__(self, violations: list[Exception], **context) -> None:
        self.violations = violations
        messages = [str(v) for v in violations]
        combined = f"{len(violations)} enforcement violation(s): " + "; ".join(messages)
        super().__init__(combined, **context)


# ---------------------------------------------------------------------------
# MCP (Model Context Protocol) errors
# ---------------------------------------------------------------------------


class MCPError(VetinariError):
    """Error in MCP client/server communication."""


# ---------------------------------------------------------------------------
# Evidence / claim-gate errors
# ---------------------------------------------------------------------------


class RecycleFailedAbort(VetinariError):
    """Raised when RecycleStore.retire fails and the destructive op is aborted.

    The decorated function is never called.  The original target is untouched
    because the recycle store rolls back on failure.  The caller should surface
    this to the user with guidance to investigate disk/permission issues before
    retrying the destructive operation.

    Args:
        message: Human-readable description of why the recycle failed.
        target: String path of the file/directory that could not be recycled.
    """

    def __init__(self, message: str, *, target: str) -> None:
        super().__init__(message, target=target)
        self.target = target


class InsufficientEvidenceError(VetinariError):
    """Raised when an OutcomeSignal cannot be constructed due to inadequate evidence.

    The most common trigger is constructing a HUMAN_ATTESTED OutcomeSignal
    without any AttestedArtifact on a path that requires tool-backed evidence
    (e.g., promotion audit, release proof, high-accuracy factual claim closure).

    Bare human attestation ("a user said yes") is valid only for intent
    confirmation (destructive-op consent, override appeal) and must be
    accompanied by ``use_case="INTENT_CONFIRMATION"`` on the signal.

    Args:
        message: Human-readable description of what evidence is missing.
        basis: The EvidenceBasis value that triggered the error.
        use_case: The use-case label on the signal being constructed.
    """


# ---------------------------------------------------------------------------
# OWASP LLM Top-10 security errors (LLM01/04/06/07/10)
# ---------------------------------------------------------------------------


class ProvenanceValidationError(SecurityError):
    """Raised when a tuning data source fails SHA-256 digest verification (LLM01).

    Failing closed here prevents poisoned or tampered training data from
    entering the intake pipeline and corrupting future model behaviour.

    Args:
        message: Human-readable description of the provenance failure.
        source_id: Identifier of the source record that failed validation.
        expected_digest: The expected SHA-256 hex digest.
        actual_digest: The computed digest found during validation.
    """

    def __init__(
        self,
        message: str = "",
        *,
        source_id: str = "",
        expected_digest: str = "",
        actual_digest: str = "",
        **context,
    ) -> None:
        super().__init__(
            message, source_id=source_id, expected_digest=expected_digest, actual_digest=actual_digest, **context
        )
        self.source_id = source_id
        self.expected_digest = expected_digest
        self.actual_digest = actual_digest


class ScopeViolationError(SecurityError):
    """Raised when an agent action exceeds the granted autonomy scope (LLM04).

    Failing closed here prevents autonomous agents from taking actions beyond
    what a human operator explicitly authorized, blocking privilege escalation.

    Args:
        message: Human-readable description of the scope violation.
        action: The action that was attempted.
        granted_scope: The scope that was actually authorized.
    """

    def __init__(
        self,
        message: str = "",
        *,
        action: str = "",
        granted_scope: str = "",
        **context,
    ) -> None:
        super().__init__(message, action=action, granted_scope=granted_scope, **context)
        self.action = action
        self.granted_scope = granted_scope


class SystemPromptLeakageError(SecurityError):
    """Raised when a message sequence risks leaking system prompt content (LLM06).

    System prompts may contain internal instructions, credentials, or policy
    details.  Isolation is mandatory to prevent exfiltration via model outputs.

    Args:
        message: Human-readable description of the isolation violation.
        leakage_type: Category of leakage detected (e.g., 'injection', 'echo').
    """

    def __init__(self, message: str = "", *, leakage_type: str = "", **context) -> None:
        super().__init__(message, leakage_type=leakage_type, **context)
        self.leakage_type = leakage_type


class OutputBudgetExceededError(SecurityError):
    """Raised when a request would exceed the per-user token output budget (LLM07).

    Unbounded output budgets enable data exfiltration and resource exhaustion.
    Fail closed: the request is rejected rather than allowing partial overrun.

    Args:
        message: Human-readable description of the budget violation.
        user_id: The user whose budget was exceeded.
        tokens_requested: Number of tokens requested.
        budget: Maximum tokens allowed for this user.
    """

    def __init__(
        self,
        message: str = "",
        *,
        user_id: str = "",
        tokens_requested: int = 0,
        budget: int = 0,
        **context,
    ) -> None:
        super().__init__(message, user_id=user_id, tokens_requested=tokens_requested, budget=budget, **context)
        self.user_id = user_id
        self.tokens_requested = tokens_requested
        self.budget = budget


class RemediationBypassError(SecurityError):
    """Raised when ``log_only=True`` is used without explicit authorization (LLM safety).

    Remediation actions that only log without applying fixes bypass the safety
    guarantee.  This error enforces that the bypass is intentional and recorded.

    Args:
        message: Human-readable description of the bypass attempt.
        action_name: The remediation action that was bypassed.
    """

    def __init__(self, message: str = "", *, action_name: str = "", **context) -> None:
        super().__init__(message, action_name=action_name, **context)
        self.action_name = action_name


class SigningUnavailableError(SecurityError):
    """Raised when a required GPG signing operation fails or GPG is unavailable.

    LoRA adapter artifacts must be signed before release so downstream consumers
    can verify authenticity.  Failing closed prevents unsigned artifacts from
    entering distribution channels.

    Args:
        message: Human-readable description of why signing failed.
        artifact_path: Path of the artifact that could not be signed.
    """

    def __init__(self, message: str = "", *, artifact_path: str = "", **context) -> None:
        super().__init__(message, artifact_path=artifact_path, **context)
        self.artifact_path = artifact_path
