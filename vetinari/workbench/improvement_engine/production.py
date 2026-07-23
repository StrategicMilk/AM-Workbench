"""Production call sites for governed improvement candidates."""

from __future__ import annotations

from pathlib import Path

from vetinari.workbench.improvement_engine.contracts import ImprovementCandidate, ImprovementDecision
from vetinari.workbench.improvement_engine.runtime import ImprovementEngineStore, load_improvement_engine_policy


def submit_workbench_candidate(
    candidate: ImprovementCandidate,
    *,
    state_root: str | Path | None = None,
) -> ImprovementDecision:
    """Submit a candidate through the governed improvement-engine store.

    Args:
        candidate: Candidate value consumed by submit_workbench_candidate().
        state_root: State root value consumed by submit_workbench_candidate().

    Returns:
        ImprovementDecision value produced by submit_workbench_candidate().
    """
    policy = load_improvement_engine_policy()
    root = Path(state_root) if state_root is not None else Path(policy.default_state_dir)
    return ImprovementEngineStore(root, state_filename=policy.state_filename).submit_candidate(candidate)


__all__ = ["submit_workbench_candidate"]
