"""Reusable architecture-contract probe enforcement for Workbench surfaces."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass


class ArchitectureContractError(AssertionError):
    """Raised when a contract probe does not prove the intended branch."""


class ContractViolation(ValueError):
    """Raised when a runtime architecture contract is violated."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ArchitectureContractProbeResult:
    """Structured result from one branch-discriminating contract probe."""

    subject: str
    branch: str
    trusted: bool
    reasons: tuple[str, ...]
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.subject.strip():
            raise ArchitectureContractError("contract probe subject is required")
        if not self.branch.strip():
            raise ArchitectureContractError(f"{self.subject}: contract probe branch is required")
        object.__setattr__(self, "reasons", _clean_tuple(self.reasons))
        object.__setattr__(self, "evidence_refs", _clean_tuple(self.evidence_refs))

    def to_dict(self) -> dict[str, object]:
        return {
            "subject": self.subject,
            "branch": self.branch,
            "trusted": self.trusted,
            "reasons": list(self.reasons),
            "evidence_refs": list(self.evidence_refs),
        }

    def __repr__(self) -> str:
        """Return a compact probe summary for failed contract assertions."""
        return (
            f"ArchitectureContractProbeResult(branch={self.branch!r}, trusted={self.trusted!r}, "
            f"reason_count={len(self.reasons)!r}, evidence_ref_count={len(self.evidence_refs)!r})"
        )


def trusted_contract_result(
    subject: str,
    *,
    branch: str,
    evidence_refs: Iterable[str],
    reasons: Iterable[str] = (),
) -> ArchitectureContractProbeResult:
    """Return a trusted branch result with mandatory evidence.

    Returns:
        Trusted architecture contract probe result.
    """
    result = ArchitectureContractProbeResult(
        subject=subject,
        branch=branch,
        trusted=True,
        reasons=tuple(reasons),
        evidence_refs=tuple(evidence_refs),
    )
    require_trusted(result)
    return result


def blocked_contract_result(
    subject: str,
    *,
    branch: str,
    reasons: Iterable[str],
    evidence_refs: Iterable[str],
) -> ArchitectureContractProbeResult:
    """Return a fail-closed branch result with explicit blockers.

    Returns:
        Blocked architecture contract probe result.
    """
    result = ArchitectureContractProbeResult(
        subject=subject,
        branch=branch,
        trusted=False,
        reasons=tuple(reasons),
        evidence_refs=tuple(evidence_refs),
    )
    require_fail_closed(result)
    return result


def require_trusted(result: ArchitectureContractProbeResult) -> None:
    """Require a positive contract branch to carry direct evidence.

    Raises:
        ArchitectureContractError: If the branch is blocked or lacks evidence.
    """
    if not result.trusted:
        raise ArchitectureContractError(
            f"{result.subject}:{result.branch} expected trusted branch, got blockers {result.reasons!r}"
        )
    if not result.evidence_refs:
        raise ArchitectureContractError(f"{result.subject}:{result.branch} trusted branch lacks evidence")


def require_fail_closed(
    result: ArchitectureContractProbeResult,
    *,
    expected_reason: str | None = None,
) -> None:
    """Require an unsafe or unknown branch to fail closed with evidence.

    Raises:
        ArchitectureContractError: If the branch is trusted, lacks blockers, or lacks evidence.
    """
    if result.trusted:
        raise ArchitectureContractError(f"{result.subject}:{result.branch} unexpectedly trusted")
    if not result.reasons:
        raise ArchitectureContractError(f"{result.subject}:{result.branch} blocked branch lacks reason")
    if expected_reason is not None and expected_reason not in result.reasons:
        raise ArchitectureContractError(
            f"{result.subject}:{result.branch} missing expected blocker {expected_reason!r}; got {result.reasons!r}"
        )
    if not result.evidence_refs:
        raise ArchitectureContractError(f"{result.subject}:{result.branch} blocked branch lacks evidence")


def assert_probe_matrix(
    results: Iterable[ArchitectureContractProbeResult],
) -> tuple[ArchitectureContractProbeResult, ...]:
    """Require a probe set to include both trusted and fail-closed branches.

    Returns:
        Materialized probe results after validation.

    Raises:
        ArchitectureContractError: If the probe set is empty or missing a required branch.
    """
    materialized = tuple(results)
    if not materialized:
        raise ArchitectureContractError("architecture contract probe matrix is empty")
    if not any(result.trusted for result in materialized):
        raise ArchitectureContractError("architecture contract probe matrix lacks a trusted branch")
    if not any(not result.trusted for result in materialized):
        raise ArchitectureContractError("architecture contract probe matrix lacks a fail-closed branch")
    for result in materialized:
        if result.trusted:
            require_trusted(result)
        else:
            require_fail_closed(result)
    return materialized


def strict_probe_or_raise(probe_fn: Callable[[], object], *, extension_id: str, endpoint: str) -> None:
    """Run an admission probe and convert any failure into a contract violation.

    Raises:
        ContractViolation: If the probe raises any exception.
    """
    try:
        probe_fn()
    except Exception as exc:
        raise ContractViolation(
            "transport-probe-failed",
            f"extension={extension_id} endpoint={endpoint} cause={type(exc).__name__}: {exc}",
        ) from exc


def _clean_tuple(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(str(value).strip() for value in values if str(value).strip())
