"""Workbench playground scratch-state runner.

This is step 8 in the Workbench improvement spine: it applies prompt, agent,
tool, and model edits to trace-derived eval cases and holds those experiments
in process memory. Experiments are non-durable scratch state until explicit
promotion. Promotion appends one new ``WorkbenchRun`` and one new
``EvalResult`` through ``WorkbenchSpine.append_run`` and ``append_eval``.
Existing spine eval rows are never mutated by this module.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final

from vetinari.types import AgentType
from vetinari.workbench.evals import EvalKind, EvalResult, EvalScore
from vetinari.workbench.metadata_spine import WorkbenchSpine, WorkbenchSpineCorrupt, get_workbench_spine
from vetinari.workbench.runs import RunKind, RunStatus, WorkbenchRun
from vetinari.workbench.trace_to_eval import ReplayScaffold, TraceEvalFactory, TraceEvalFactoryError
from vetinari.workbench.traces import WorkbenchTrace

logger = logging.getLogger(__name__)


_SCRATCH_STATE_KEY: Final[str] = "playground_scratch"
_MAX_SCRATCH_EXPERIMENTS: Final[int] = 1000
_PROMOTION_PASSING_THRESHOLD: Final[float] = 0.5
_PLAYGROUND_INSTANCE: Playground | None = None
_PLAYGROUND_LOCK: threading.Lock = threading.Lock()


class PlaygroundError(Exception):
    """Typed playground boundary error."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason

    def __str__(self) -> str:
        return f"{self.args[0]} (reason={self.reason})"


@dataclass(frozen=True, slots=True)
class PlaygroundExperiment:
    """One in-memory playground experiment ready for optional promotion."""

    experiment_id: str
    source_trace_id: str
    source_run_id: str
    project_id: str
    asset_id: str
    asset_revision: str
    prompt_text: str
    agent_edits: tuple[str, ...]
    tool_overrides: tuple[str, ...]
    model_overrides: tuple[str, ...]
    created_at_utc: str
    score: float = 0.0
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.experiment_id.strip():
            raise ValueError("experiment_id must be non-empty")
        if not self.source_trace_id.strip():
            raise ValueError("source_trace_id must be non-empty")
        if not self.project_id.strip():
            raise ValueError("project_id must be non-empty")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"PlaygroundExperiment(experiment_id={self.experiment_id!r}, source_trace_id={self.source_trace_id!r}, source_run_id={self.source_run_id!r})"


class Playground:
    """In-memory experiment runner guarded by scratch-state locks.

    ``_scratch_lock`` protects the experiment dictionary and the full promotion
    critical section. Promoting the same experiment twice appends a second new
    run and eval pair because each promotion is a distinct evidence event.
    """

    def __init__(
        self,
        *,
        spine: WorkbenchSpine | None = None,
        factory: TraceEvalFactory | None = None,
    ) -> None:
        self._spine = spine or get_workbench_spine()
        self._factory = factory or TraceEvalFactory(self._spine)
        self._experiments: dict[str, PlaygroundExperiment] = {}
        self._scratch_lock = threading.Lock()

    def dispatch_trace_to_eval(
        self,
        trace: WorkbenchTrace,
        *,
        asset_id: str,
        asset_revision: str,
    ) -> tuple[EvalResult, ReplayScaffold]:
        """Convert a real trace to an eval case without mutating scratch state.

        Returns:
            tuple[EvalResult, ReplayScaffold] value produced by dispatch_trace_to_eval().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        try:
            return self._factory.promote_trace(
                trace,
                asset_id=asset_id,
                asset_revision=asset_revision,
            )
        except TraceEvalFactoryError as exc:
            raise PlaygroundError(str(exc), reason="trace-rejected") from exc

    def run_experiment(
        self,
        *,
        source_trace_id: str,
        source_run_id: str,
        project_id: str | None = None,
        asset_id: str,
        asset_revision: str,
        prompt_text: str,
        agent_edits: tuple[str, ...] = (),
        tool_overrides: tuple[str, ...] = (),
        model_overrides: tuple[str, ...] = (),
        score: float = 0.0,
        notes: str = "",
    ) -> str:
        """Record an in-memory experiment without writing to the spine.

        Returns:
            Outcome produced by run_experiment().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        experiment_id = f"pg-{uuid.uuid4().hex}"
        resolved_project_id = project_id or self._project_id_for_source_run(source_run_id)
        experiment = PlaygroundExperiment(
            experiment_id=experiment_id,
            source_trace_id=source_trace_id,
            source_run_id=source_run_id,
            project_id=resolved_project_id,
            asset_id=asset_id,
            asset_revision=asset_revision,
            prompt_text=prompt_text,
            agent_edits=agent_edits,
            tool_overrides=tool_overrides,
            model_overrides=model_overrides,
            created_at_utc=datetime.now(timezone.utc).isoformat(),
            score=score,
            notes=notes,
        )
        with self._scratch_lock:
            if len(self._experiments) >= _MAX_SCRATCH_EXPERIMENTS:
                raise PlaygroundError(
                    f"playground scratch experiment cap reached ({_MAX_SCRATCH_EXPERIMENTS})",
                    reason="scratch-cap-reached",
                )
            self._experiments[experiment_id] = experiment
        return experiment_id

    def list_experiments(self) -> list[PlaygroundExperiment]:
        """Return a lock-protected snapshot of scratch experiments.

        Returns:
            Collection of experiments values.
        """
        with self._scratch_lock:
            return list(self._experiments.values())

    def get_experiment(self, experiment_id: str) -> PlaygroundExperiment:
        """Return one scratch experiment or raise a typed error.

        Returns:
            Resolved experiment value.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        with self._scratch_lock:
            try:
                return self._experiments[experiment_id]
            except KeyError as exc:
                raise PlaygroundError(
                    f"unknown experiment {experiment_id}",
                    reason="unknown-experiment",
                ) from exc

    def promote_experiment_to_spine(self, experiment_id: str) -> tuple[str, str]:
        """Append one playground run and one eval result for an experiment.

        Returns:
            tuple[str, str] value produced by promote_experiment_to_spine().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        with self._scratch_lock:
            try:
                experiment = self._experiments[experiment_id]
            except KeyError as exc:
                raise PlaygroundError(
                    f"unknown experiment {experiment_id}",
                    reason="unknown-experiment",
                ) from exc
            now = datetime.now(timezone.utc).isoformat()
            suffix = uuid.uuid4().hex[:12]
            run = WorkbenchRun(
                run_id=f"pg-run-{experiment_id}-{suffix}",
                kind=RunKind.PLAYGROUND_RUN,
                status=RunStatus.SUCCEEDED,
                started_at_utc=now,
                finished_at_utc=now,
                actor_agent_type=AgentType.WORKBENCH,
                asset_revisions=((experiment.asset_id, experiment.asset_revision),),
                lease_id="",
                shard_kind=None,
                metrics=(),
                outcome=None,
                project_id=experiment.project_id,
            )
            eval_result = EvalResult(
                eval_id=f"pg-eval-{experiment_id}-{suffix}",
                kind=EvalKind.LIVE_TRACE_DERIVED,
                run_id=run.run_id,
                asset_id=experiment.asset_id,
                asset_revision=experiment.asset_revision,
                scores=(
                    EvalScore(
                        metric_name="playground_score",
                        value=experiment.score,
                        threshold=_PROMOTION_PASSING_THRESHOLD,
                        passed=experiment.score >= _PROMOTION_PASSING_THRESHOLD,
                        unit="",
                    ),
                ),
                captured_at_utc=now,
                notes=experiment.notes or f"playground promotion of experiment {experiment_id}",
            )
            try:
                self._spine.append_run(run)
                self._spine.append_eval(eval_result)
            except WorkbenchSpineCorrupt as exc:
                raise PlaygroundError("spine append failed", reason="spine-write-failed") from exc
            return run.run_id, eval_result.eval_id

    def _project_id_for_source_run(self, source_run_id: str) -> str:
        for run in self._spine.list_runs():
            if run.run_id == source_run_id:
                return run.project_id
        raise PlaygroundError("source run project scope is unavailable", reason="project-scope-missing")


def get_playground(spine: WorkbenchSpine | None = None) -> Playground:
    """Return the process-wide Playground singleton.

    Returns:
        Resolved playground value.
    """
    global _PLAYGROUND_INSTANCE
    if _PLAYGROUND_INSTANCE is None:
        with _PLAYGROUND_LOCK:
            if _PLAYGROUND_INSTANCE is None:
                _PLAYGROUND_INSTANCE = Playground(spine=spine)
    return _PLAYGROUND_INSTANCE


def reset_playground_for_test() -> None:
    """Clear the playground singleton for isolated tests."""
    global _PLAYGROUND_INSTANCE
    with _PLAYGROUND_LOCK:
        _PLAYGROUND_INSTANCE = None


__all__ = [
    "_MAX_SCRATCH_EXPERIMENTS",
    "_PROMOTION_PASSING_THRESHOLD",
    "Playground",
    "PlaygroundError",
    "PlaygroundExperiment",
    "get_playground",
    "reset_playground_for_test",
]
