"""Deterministic Workbench sweep scheduling and evidence records."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from vetinari.agents.contracts import OutcomeSignal
from vetinari.types import AgentType, ShardKind
from vetinari.utils.bounded_collections import BoundedList
from vetinari.workbench.evals import EvalKind, EvalResult, EvalScore
from vetinari.workbench.experiments import ExperimentVariant, TerminationReason, WorkbenchExperiment
from vetinari.workbench.runs import RunKind, RunMetric, RunStatus, WorkbenchRun
from vetinari.workbench.spine import WorkbenchProjectIdRejected, validate_project_id
from vetinari.workbench.spine_consumers import record_eval_written

_SWEEP_STORE_LOCK: threading.Lock = threading.Lock()


class SweepStoreError(RuntimeError):
    """Raised when sweep state cannot be safely read or written."""


class SweepStatus(str, Enum):
    """Lifecycle state for a sweep record."""

    PENDING = "pending"
    RUNNING = "running"
    TERMINATED = "terminated"


@dataclass(frozen=True, slots=True)
class SweepTrial:
    """One scheduled candidate trial and its measured evidence."""

    trial_id: str
    variant_id: str
    run: WorkbenchRun
    eval_result: EvalResult
    cost_usd: float
    latency_ms: float
    resource_metrics: tuple[RunMetric, ...]
    artifact_refs: tuple[str, ...]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SweepTrial(trial_id={self.trial_id!r}, variant_id={self.variant_id!r}, run={self.run!r})"


@dataclass(frozen=True, slots=True)
class SweepRecord:
    """Persistable sweep state."""

    sweep_id: str
    project_id: str
    experiment_id: str
    status: SweepStatus
    scheduled_variant_ids: tuple[str, ...]
    trials: tuple[SweepTrial, ...]
    budget: dict[str, float | int | None]
    termination_reason: TerminationReason
    baseline_ref: str
    candidate_ref: str

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SweepRecord(sweep_id={self.sweep_id!r}, project_id={self.project_id!r}, experiment_id={self.experiment_id!r})"


class SweepScheduler:
    """Deterministic scheduler for bounded experiment sweeps."""

    def __init__(self, experiment: WorkbenchExperiment) -> None:
        self.experiment = experiment

    def candidate_variants(self) -> tuple[ExperimentVariant, ...]:
        """Execute the candidate variants operation.

        Returns:
            tuple[ExperimentVariant, ...] value produced by candidate_variants().
        """
        candidates = tuple(self.experiment.search_space)
        if self.experiment.scheduler.value == "lowest_cost_first":
            return tuple(sorted(candidates, key=lambda variant: float(variant.parameters.get("estimated_cost_usd", 0))))
        return candidates

    def next_candidate(self, completed_variant_ids: set[str] | None = None) -> ExperimentVariant | None:
        """Execute the next candidate operation.

        Returns:
            ExperimentVariant | None value produced by next_candidate().
        """
        completed = completed_variant_ids or set()
        for variant in self.candidate_variants():
            if variant.variant_id not in completed:
                return variant
        return None

    def run_bounded(
        self,
        *,
        sweep_id: str,
        project_id: str,
        method_metrics: tuple[RunMetric, ...] = (),
    ) -> SweepRecord:
        """Execute the run bounded operation.

        Returns:
            Outcome produced by run_bounded().
        """
        project_id = validate_project_id(project_id)
        trials = BoundedList[SweepTrial](max(1, self.experiment.budget.max_trials))
        total_cost = 0.0
        candidates = self.candidate_variants()
        completed_variant_ids: set[str] = set()
        termination = TerminationReason.NO_CANDIDATES
        index = 0
        while len(completed_variant_ids) < len(candidates):
            variant = self.next_candidate(completed_variant_ids)
            if variant is None:
                break
            index += 1
            if len(trials) >= self.experiment.budget.max_trials:
                termination = TerminationReason.MAX_TRIALS_REACHED
                break
            candidate_cost = float(variant.parameters.get("estimated_cost_usd", 0.0))
            if total_cost + candidate_cost > self.experiment.budget.max_cost_usd:
                termination = TerminationReason.BUDGET_EXHAUSTED
                break
            candidate_latency = float(
                variant.parameters.get("latency_ms", self.experiment.budget.max_latency_ms or 0.0)
            )
            if (
                self.experiment.budget.max_latency_ms is not None
                and candidate_latency > self.experiment.budget.max_latency_ms
            ):
                termination = TerminationReason.SAFETY_CONSTRAINTS
                break
            trial = _build_trial(
                sweep_id=sweep_id,
                project_id=project_id,
                experiment=self.experiment,
                variant=variant,
                index=index,
                cost_usd=candidate_cost,
                method_metrics=method_metrics,
            )
            trials.append(trial)
            completed_variant_ids.add(variant.variant_id)
            total_cost += candidate_cost
            termination = TerminationReason.MAX_TRIALS_REACHED
            if _objective_met(self.experiment, trial.eval_result):
                termination = TerminationReason.OBJECTIVE_MET
                break
        return SweepRecord(
            sweep_id=sweep_id,
            project_id=project_id,
            experiment_id=self.experiment.experiment_id,
            status=SweepStatus.TERMINATED,
            scheduled_variant_ids=tuple(variant.variant_id for variant in candidates),
            trials=tuple(trials),
            budget={
                "max_trials": self.experiment.budget.max_trials,
                "max_cost_usd": self.experiment.budget.max_cost_usd,
                "max_latency_ms": self.experiment.budget.max_latency_ms,
                "spent_cost_usd": total_cost,
            },
            termination_reason=termination,
            baseline_ref=self.experiment.baseline_ref,
            candidate_ref=self.experiment.candidate_ref,
        )


class SweepStore:
    """Lock-guarded append-only JSONL store for sweep records."""

    def __init__(self, root: Path | str, *, max_records: int = 1_000) -> None:
        self.root = Path(root)
        self.max_records = max(1, int(max_records))

    def append_record(self, project_id: str, record: SweepRecord) -> None:
        """Execute the append record operation.

        Args:
            project_id: Project identifier that scopes the operation.
            record: Typed record consumed by the operation.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        project_id = validate_project_id(project_id)
        if record.project_id != project_id:
            raise SweepStoreError("record project_id mismatch")
        path = self._path(project_id)
        with _SWEEP_STORE_LOCK:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8", newline="\n") as fh:
                    fh.write(json.dumps(_record_to_payload(record), sort_keys=True) + "\n")
                # spine_consumers invokes get_spine() and absorbs observability failures.
                record_eval_written(
                    eval_id=record.sweep_id,
                    project_id=record.project_id,
                    score=_record_score(record),
                )
            except OSError as exc:
                raise SweepStoreError(f"sweep store append failed: {path}") from exc

    def list_records(self, project_id: str) -> tuple[SweepRecord, ...]:
        """Execute the list records operation.

        Returns:
            Collection of records values.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        project_id = validate_project_id(project_id)
        path = self._path(project_id)
        with _SWEEP_STORE_LOCK:
            if not path.exists():
                raise SweepStoreError(f"sweep store missing: {path}")
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                raise SweepStoreError(f"sweep store unreadable: {path}") from exc
            records = BoundedList[SweepRecord](self.max_records)
            for lineno, line in enumerate(lines, start=1):
                if not line.strip():
                    raise SweepStoreError(f"sweep store corrupt blank line {lineno}: {path}")
                try:
                    payload = json.loads(line)
                    records.append(_record_from_payload(payload))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise SweepStoreError(f"sweep store corrupt line {lineno}: {path}") from exc
            return tuple(records)

    def _path(self, project_id: str) -> Path:
        try:
            safe_project_id = validate_project_id(project_id)
        except WorkbenchProjectIdRejected:
            raise
        return self.root / safe_project_id / "sweeps.jsonl"


def _build_trial(
    *,
    sweep_id: str,
    project_id: str,
    experiment: WorkbenchExperiment,
    variant: ExperimentVariant,
    index: int,
    cost_usd: float,
    method_metrics: tuple[RunMetric, ...],
) -> SweepTrial:
    run_id = f"{sweep_id}-{variant.variant_id}-run"
    latency_ms = float(variant.parameters.get("latency_ms", experiment.budget.max_latency_ms or 0.0))
    candidate_score = float(variant.parameters.get("score", experiment.objective.target))
    eval_passed = (
        candidate_score >= experiment.objective.target
        if experiment.objective.higher_is_better
        else candidate_score <= experiment.objective.target
    )
    metrics = (
        RunMetric(name="baseline_score", value=experiment.objective.target, unit=experiment.objective.unit),
        RunMetric(name="candidate_score", value=candidate_score, unit=experiment.objective.unit),
        RunMetric(name="cost_usd", value=cost_usd, unit="usd"),
        RunMetric(name="latency_ms", value=latency_ms, unit="ms"),
        *method_metrics,
    )
    started_at = _trial_timestamp(index, offset_seconds=0)
    finished_at = _trial_timestamp(index, offset_seconds=60)
    captured_at = _trial_timestamp(index, offset_seconds=120)
    run = WorkbenchRun(
        run_id=run_id,
        kind=RunKind.EVAL_RUN,
        status=RunStatus.SUCCEEDED if eval_passed else RunStatus.FAILED,
        started_at_utc=started_at,
        finished_at_utc=finished_at,
        actor_agent_type=AgentType.WORKBENCH,
        asset_revisions=tuple((ref, "current") for ref in variant.asset_refs),
        lease_id="sweep-local",
        shard_kind=ShardKind.STANDARD,
        metrics=metrics,
        outcome=OutcomeSignal(passed=eval_passed, score=candidate_score),
        project_id=project_id,
    )
    eval_result = EvalResult(
        eval_id=f"{sweep_id}-{variant.variant_id}-eval",
        kind=EvalKind.OFFLINE_SUITE,
        run_id=run_id,
        asset_id=variant.asset_refs[0] if variant.asset_refs else variant.variant_id,
        asset_revision="current",
        scores=(
            EvalScore(
                metric_name=experiment.objective.name,
                value=candidate_score,
                threshold=experiment.objective.target,
                passed=eval_passed,
                unit=experiment.objective.unit,
            ),
        ),
        captured_at_utc=captured_at,
    )
    return SweepTrial(
        trial_id=f"{sweep_id}-{variant.variant_id}",
        variant_id=variant.variant_id,
        run=run,
        eval_result=eval_result,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        resource_metrics=tuple(metric for metric in metrics if metric.name in {"cost_usd", "latency_ms"}),
        artifact_refs=variant.asset_refs,
    )


def _trial_timestamp(index: int, *, offset_seconds: int) -> str:
    timestamp = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(
        seconds=max(index - 1, 0) + offset_seconds
    )
    return timestamp.isoformat().replace("+00:00", "Z")


def _objective_met(experiment: WorkbenchExperiment, eval_result: EvalResult) -> bool:
    score = eval_result.scores[0]
    return bool(score.passed)


def _record_to_payload(record: SweepRecord) -> dict[str, Any]:
    return {
        "sweep_id": record.sweep_id,
        "project_id": record.project_id,
        "experiment_id": record.experiment_id,
        "status": record.status.value,
        "scheduled_variant_ids": list(record.scheduled_variant_ids),
        "trials": [
            {
                "trial_id": trial.trial_id,
                "variant_id": trial.variant_id,
                "run": _run_to_payload(trial.run),
                "eval_result": _eval_to_payload(trial.eval_result),
                "cost_usd": trial.cost_usd,
                "latency_ms": trial.latency_ms,
                "resource_metrics": [_metric_to_payload(metric) for metric in trial.resource_metrics],
                "artifact_refs": list(trial.artifact_refs),
            }
            for trial in record.trials
        ],
        "budget": dict(record.budget),
        "termination_reason": record.termination_reason.value,
        "baseline_ref": record.baseline_ref,
        "candidate_ref": record.candidate_ref,
    }


def _record_score(record: SweepRecord) -> float | None:
    if not record.trials:
        return None
    scores = record.trials[-1].eval_result.scores
    if not scores:
        return None
    return float(scores[0].value)


def _run_to_payload(run: WorkbenchRun) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "kind": run.kind.value,
        "status": run.status.value,
        "started_at_utc": run.started_at_utc,
        "finished_at_utc": run.finished_at_utc,
        "actor_agent_type": run.actor_agent_type.value,
        "asset_revisions": [list(row) for row in run.asset_revisions],
        "lease_id": run.lease_id,
        "shard_kind": run.shard_kind.value if run.shard_kind is not None else None,
        "metrics": [_metric_to_payload(metric) for metric in run.metrics],
        "project_id": run.project_id,
    }


def _eval_to_payload(eval_result: EvalResult) -> dict[str, Any]:
    return {
        "eval_id": eval_result.eval_id,
        "kind": eval_result.kind.value,
        "run_id": eval_result.run_id,
        "asset_id": eval_result.asset_id,
        "asset_revision": eval_result.asset_revision,
        "scores": [
            {
                "metric_name": score.metric_name,
                "value": score.value,
                "threshold": score.threshold,
                "passed": score.passed,
                "unit": score.unit,
            }
            for score in eval_result.scores
        ],
        "captured_at_utc": eval_result.captured_at_utc,
        "notes": eval_result.notes,
    }


def _metric_to_payload(metric: RunMetric) -> dict[str, Any]:
    return {"name": metric.name, "value": metric.value, "unit": metric.unit}


def _record_from_payload(payload: dict[str, Any]) -> SweepRecord:
    trials = tuple(
        SweepTrial(
            trial_id=trial["trial_id"],
            variant_id=trial["variant_id"],
            run=_run_from_payload(trial["run"]),
            eval_result=_eval_from_payload(trial["eval_result"]),
            cost_usd=float(trial["cost_usd"]),
            latency_ms=float(trial["latency_ms"]),
            resource_metrics=tuple(
                RunMetric(name=row["name"], value=float(row["value"]), unit=row.get("unit", ""))
                for row in trial["resource_metrics"]
            ),
            artifact_refs=tuple(trial["artifact_refs"]),
        )
        for trial in payload["trials"]
    )
    return SweepRecord(
        sweep_id=payload["sweep_id"],
        project_id=payload["project_id"],
        experiment_id=payload["experiment_id"],
        status=SweepStatus(payload["status"]),
        scheduled_variant_ids=tuple(payload["scheduled_variant_ids"]),
        trials=trials,
        budget=dict(payload["budget"]),
        termination_reason=TerminationReason(payload["termination_reason"]),
        baseline_ref=payload["baseline_ref"],
        candidate_ref=payload["candidate_ref"],
    )


def _run_from_payload(payload: dict[str, Any]) -> WorkbenchRun:
    return WorkbenchRun(
        run_id=payload["run_id"],
        kind=RunKind(payload["kind"]),
        status=RunStatus(payload["status"]),
        started_at_utc=payload["started_at_utc"],
        finished_at_utc=payload["finished_at_utc"],
        actor_agent_type=AgentType(payload["actor_agent_type"]),
        asset_revisions=tuple(tuple(row) for row in payload["asset_revisions"]),
        lease_id=payload["lease_id"],
        shard_kind=ShardKind(payload["shard_kind"]) if payload["shard_kind"] is not None else None,
        metrics=tuple(
            RunMetric(name=row["name"], value=float(row["value"]), unit=row.get("unit", ""))
            for row in payload["metrics"]
        ),
        outcome=None,
        project_id=payload["project_id"],
    )


def _eval_from_payload(payload: dict[str, Any]) -> EvalResult:
    return EvalResult(
        eval_id=payload["eval_id"],
        kind=EvalKind(payload["kind"]),
        run_id=payload["run_id"],
        asset_id=payload["asset_id"],
        asset_revision=payload["asset_revision"],
        scores=tuple(
            EvalScore(
                metric_name=row["metric_name"],
                value=float(row["value"]),
                threshold=float(row["threshold"]),
                passed=bool(row["passed"]),
                unit=row.get("unit", ""),
            )
            for row in payload["scores"]
        ),
        captured_at_utc=payload["captured_at_utc"],
        notes=payload.get("notes", ""),
    )


__all__ = [
    "_SWEEP_STORE_LOCK",
    "SweepRecord",
    "SweepScheduler",
    "SweepStatus",
    "SweepStore",
    "SweepStoreError",
    "SweepTrial",
]
