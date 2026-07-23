"""ImprovementLog proposal writer for backend tuning verdicts."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vetinari.kaizen.improvement_log import ImprovementLog, ImprovementRecord, ImprovementStatus
from vetinari.tuning.backend_tuning import BenchmarkVerdict, RollbackPlan, stable_hash

logger = logging.getLogger(__name__)
_PROPOSAL_WRITE_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class BackendTuningImprovement:
    """Serializable ImprovementLog proposal payload."""

    proposal_id: str
    hypothesis: str
    metric: str
    baseline_value: float
    target_value: float
    applied_by: str
    rollback_plan: str
    observation_window_hours: int
    notes: str

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"BackendTuningImprovement(proposal_id={self.proposal_id!r}, hypothesis={self.hypothesis!r}, metric={self.metric!r})"


@dataclass(frozen=True, slots=True)
class ProposalWriteResult:
    """Result of an attempted backend tuning proposal write."""

    status: str
    proposal_id: str
    improvement_id: str = ""
    blocked_reasons: tuple[str, ...] = ()

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ProposalWriteResult(status={self.status!r}, proposal_id={self.proposal_id!r}, improvement_id={self.improvement_id!r})"


def build_backend_tuning_improvement(verdict: BenchmarkVerdict) -> BackendTuningImprovement:
    """Build a proposed ImprovementLog record from a passing verdict.

    Returns:
        Newly constructed backend tuning improvement value.
    """
    _require_passing_verdict(verdict)
    proposal_id = backend_tuning_proposal_id(verdict)
    notes = {
        "proposal_id": proposal_id,
        "backend": verdict.backend,
        "baseline_profile": verdict.baseline_profile,
        "candidate_profile": verdict.candidate_profile,
        "baseline_hash": verdict.baseline_hash,
        "candidate_hash": verdict.candidate_hash,
        "confidence": verdict.confidence,
        "resource_cost": verdict.resource_cost,
        "rollback": {
            "target_profile": verdict.rollback.target_profile,
            "command": verdict.rollback.command,
            "window_hours": verdict.rollback.window_hours,
        },
        "regression_window": {
            "representative_task_count": verdict.representative_task_count,
            "metric_deltas": verdict.metric_deltas,
        },
        "authority_boundary": "candidate-only; no runtime backend setting was applied",
    }
    baseline_latency = max(0.0, -verdict.metric_deltas.get("latency_ms", 0.0))
    return BackendTuningImprovement(
        proposal_id=proposal_id,
        hypothesis=(
            f"Backend tuning candidate {verdict.candidate_profile} for {verdict.backend} "
            "will preserve quality while improving latency/resource use."
        ),
        metric="backend_tuning_latency_delta_ms",
        baseline_value=0.0,
        target_value=baseline_latency,
        applied_by="backend-tuning-benchmark",
        rollback_plan=_rollback_text(verdict.rollback),
        observation_window_hours=verdict.rollback.window_hours,
        notes=json.dumps(notes, sort_keys=True),
    )


def record_backend_tuning_proposal(
    verdict: BenchmarkVerdict,
    *,
    db_path: str | Path | None = None,
) -> ProposalWriteResult:
    """Idempotently write a PROPOSED ImprovementLog record for a passing verdict.

    Returns:
        Outcome produced by record_backend_tuning_proposal().
    """
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            return _record_backend_tuning_proposal_once(verdict, db_path=db_path)
        except sqlite3.DatabaseError as exc:
            last_error = exc
            if "locked" not in str(exc).lower():
                break
            time.sleep(0.05 * (attempt + 1))
    return ProposalWriteResult(
        status="blocked",
        proposal_id=backend_tuning_proposal_id(verdict),
        blocked_reasons=(f"proposal write blocked: {last_error}",),
    )


def _record_backend_tuning_proposal_once(
    verdict: BenchmarkVerdict,
    *,
    db_path: str | Path | None,
) -> ProposalWriteResult:
    try:
        improvement = build_backend_tuning_improvement(verdict)
        log = ImprovementLog(db_path)
        with _PROPOSAL_WRITE_LOCK:
            existing = _find_existing_backend_tuning_proposal(log, improvement.proposal_id)
            if existing is not None:
                return ProposalWriteResult(
                    status="already-exists",
                    proposal_id=improvement.proposal_id,
                    improvement_id=existing.id,
                )
            improvement_id = log.propose(
                hypothesis=improvement.hypothesis,
                metric=improvement.metric,
                baseline=improvement.baseline_value,
                target=improvement.target_value,
                applied_by=improvement.applied_by,
                rollback_plan=improvement.rollback_plan,
                observation_window_hours=improvement.observation_window_hours,
                notes=improvement.notes,
                improvement_id=improvement.proposal_id,
            )
        return ProposalWriteResult(
            status="written",
            proposal_id=improvement.proposal_id,
            improvement_id=improvement_id,
        )
    except sqlite3.DatabaseError:
        raise
    except (OSError, ValueError) as exc:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return ProposalWriteResult(
            status="blocked",
            proposal_id=backend_tuning_proposal_id(verdict),
            blocked_reasons=(f"proposal write blocked: {exc}",),
        )


def backend_tuning_proposal_id(verdict: BenchmarkVerdict) -> str:
    """Return deterministic identity for a benchmark-backed tuning candidate.

    Returns:
        str value produced by backend_tuning_proposal_id().
    """
    material: dict[str, Any] = {
        "backend": verdict.backend,
        "baseline_hash": verdict.baseline_hash,
        "candidate_hash": verdict.candidate_hash,
        "candidate_profile": verdict.candidate_profile,
        "representative_task_count": verdict.representative_task_count,
        "rollback_target": verdict.rollback.target_profile,
    }
    return f"BTUNE-{stable_hash(material)[:16]}"


def _find_existing_backend_tuning_proposal(log: ImprovementLog, proposal_id: str) -> ImprovementRecord | None:
    marker = f'"proposal_id": "{proposal_id}"'
    for status in ImprovementStatus:
        for improvement in log.get_improvements_by_status(status):
            if marker in improvement.notes:
                return improvement
    return None


def _require_passing_verdict(verdict: BenchmarkVerdict) -> None:
    if not verdict.passed or verdict.blockers:
        raise ValueError(f"backend tuning proposal requires passing benchmark verdict: {verdict.blockers}")
    if not verdict.rollback.target_profile or not verdict.rollback.command:
        raise ValueError("backend tuning proposal requires rollback plan")
    if verdict.representative_task_count <= 0:
        raise ValueError("backend tuning proposal requires representative task count")
    for field_name in ("baseline_hash", "candidate_hash", "confidence"):
        if not getattr(verdict, field_name):
            raise ValueError(f"backend tuning proposal missing {field_name}")


def _rollback_text(rollback: RollbackPlan) -> str:
    return f"Rollback to profile {rollback.target_profile} within {rollback.window_hours}h using: {rollback.command}"
