"""Drift subsystem wiring — integration hooks for the Vetinari startup and scheduling pipeline.

Connects DriftMonitor, SchemaValidator, ContractRegistry, and GoalTracker into the
broader pipeline so that startup code and background schedulers have single,
self-contained entry points to call. No component is initialised until these
functions are invoked, keeping import cost near zero.

Pipeline position: called at startup (wire_drift_subsystem / startup_drift_validation)
and from background scheduler tasks (schedule_drift_audit, schedule_contract_check).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from vetinari.drift.contract_registry import get_contract_registry
from vetinari.drift.goal_tracker import AdherenceResult, GoalTracker, create_goal_tracker
from vetinari.drift.monitor import DriftMonitorReport, get_drift_monitor
from vetinari.drift.schema_validator import get_schema_validator

logger = logging.getLogger(__name__)


# Adherence score below which a WARNING is emitted.
_LOW_ADHERENCE_THRESHOLD = 0.4
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA_FIXTURE_DIR = _REPO_ROOT / "tests" / "fixtures" / "schema_known_bad"
_AUDIT_GROUND_TRUTH_SCHEMA = _REPO_ROOT / "schemas" / "audit_ground_truth.schema.json"
_AUDIT_GROUND_TRUTH_SCHEMA_NAMES = frozenset({
    "audit_ground_truth",
    "audit_ground_truth.schema",
    "audit_ground_truth.schema.json",
    "schemas/audit_ground_truth.schema.json",
})


def _validate_with_known_bad(validator: object, name: str, fixture_dir: Path) -> tuple[list[str], bool]:
    """Validate a schema against an adversarial fixture when available."""
    fixture_path = fixture_dir / f"{name}.json"
    if not fixture_path.exists():
        if name in _AUDIT_GROUND_TRUTH_SCHEMA_NAMES:
            if not _AUDIT_GROUND_TRUTH_SCHEMA.exists():
                return [f"Required schema file is missing: {_AUDIT_GROUND_TRUTH_SCHEMA}"], False
            known_bad = {"disposition": "fixed", "closure_evidence": ""}
            errors = validator.validate(name, known_bad)
            if not errors:
                return [f"Known-bad audit ground truth payload validated cleanly: {_AUDIT_GROUND_TRUTH_SCHEMA}"], True
            return errors, True
        logger.warning(
            "startup_drift_validation: no known-bad fixture for schema %r - coverage incomplete",
            name,
        )
        return [f"Missing known-bad fixture for schema {name!r}: {fixture_path}"], False

    known_bad = json.loads(fixture_path.read_text(encoding="utf-8"))
    errors = validator.validate(name, known_bad)
    if not errors:
        return [f"Known-bad fixture for schema {name!r} validated cleanly: {fixture_path}"], True
    return errors, True


def startup_drift_validation() -> bool:
    """Bootstrap the drift subsystem and validate all critical schemas.

    Intended to be called once during application startup. Initialises
    DriftMonitor (which seeds the ContractRegistry and CapabilityAuditor
    baselines), then asks SchemaValidator to validate all registered schemas
    against live sample objects.

    Does NOT raise on failure — the caller decides whether a bad schema
    should abort startup or just emit warnings.

    Returns:
        True when every registered schema validates cleanly, False when one
        or more schemas report errors.
    """
    monitor = get_drift_monitor()
    monitor.bootstrap()

    validator = get_schema_validator()
    schema_names = validator.list_schemas()
    if not schema_names:
        logger.info("startup_drift_validation: no schemas registered yet, skipping validation")
        return True

    all_valid = True
    fixture_dir = _SCHEMA_FIXTURE_DIR
    for name in schema_names:
        errors, exercised_known_bad = _validate_with_known_bad(validator, name, fixture_dir)
        if errors:
            if exercised_known_bad and all(error.startswith("Known-bad fixture") for error in errors):
                logger.warning(
                    "startup_drift_validation: schema '%s' known-bad coverage failed: %s",
                    name,
                    errors,
                )
                all_valid = False
            elif exercised_known_bad:
                logger.info(
                    "startup_drift_validation: schema '%s' rejected known-bad fixture with %d issue(s)",
                    name,
                    len(errors),
                )
            else:
                logger.warning(
                    "startup_drift_validation: schema '%s' validation failed without known-bad fixture: %s",
                    name,
                    errors,
                )
                all_valid = False

    if all_valid:
        logger.info("startup_drift_validation: all %d schemas valid", len(schema_names))
    return all_valid


def schedule_drift_audit() -> DriftMonitorReport:
    """Run a full drift audit across contracts, capabilities, and schemas.

    Intended to be invoked periodically (e.g. every 6 hours) by a background
    scheduler. Delegates to DriftMonitor.run_full_audit() and logs a summary
    at INFO when clean or WARNING when drift is detected.

    Returns:
        The DriftReport produced by this audit cycle.
    """
    monitor = get_drift_monitor()
    report = monitor.run_full_audit()

    if report.is_clean:
        logger.info("Scheduled drift audit complete: %s", report.summary())
    else:
        logger.warning(
            "Scheduled drift audit found issues (%d): %s",
            len(report.issues),
            report.summary(),
        )
        for issue in report.issues:
            logger.warning("  drift issue: %s", issue)

    return report


def schedule_contract_check() -> dict[str, dict[str, str]]:
    """Check contract fingerprint drift and snapshot the registry if clean.

    Intended to be called in CI pipelines or by a background scheduler.
    Loads the last persisted snapshot, compares it against current fingerprints,
    and — if no drift is found — writes a fresh snapshot so the current state
    becomes the new baseline.

    Returns:
        Mapping of contract name to ``{"previous": hash, "current": hash}``
        for every contract whose fingerprint has changed. Empty dict means all
        contracts are stable.
    """
    registry = get_contract_registry()
    registry.load_snapshot()
    drifts = registry.check_drift()

    if drifts:
        logger.warning(
            "Contract check: %d drifted contract(s): %s",
            len(drifts),
            list(drifts.keys()),
        )
    else:
        registry.snapshot()
        logger.info(
            "Contract check clean (%d contracts); snapshot updated",
            len(registry.list_contracts()),
        )

    return drifts


def check_goal_adherence(
    original_goal: str,
    task_output: str,
    task_description: str,
) -> AdherenceResult:
    """Verify that a task output still aligns with the original goal.

    Creates a short-lived GoalTracker for the given goal (one tracker per
    call — GoalTracker is not a singleton) and runs a single adherence check.
    Logs a WARNING when the adherence score falls below the low-adherence
    threshold, indicating meaningful drift from the stated goal.

    Args:
        original_goal: The top-level goal the task is meant to serve.
        task_output: The output text produced by the task.
        task_description: Human-readable description of what the task did.

    Returns:
        AdherenceResult containing a 0.0-1.0 alignment score, deviation
        description, corrective suggestion, and keyword match counts.
    """
    tracker: GoalTracker = create_goal_tracker(original_goal)
    result = tracker.check_adherence(task_output, task_description)

    if result.score < _LOW_ADHERENCE_THRESHOLD:
        logger.warning(
            "Goal adherence low (score=%.3f) for task '%s': %s",
            result.score,
            task_description[:80],
            result.deviation_description,
        )
    else:
        logger.debug(
            "Goal adherence ok (score=%.3f, matched=%d/%d) for task '%s'",
            result.score,
            result.keywords_matched,
            result.keywords_total,
            task_description[:80],
        )

    return result


def get_active_drift_trend() -> dict:
    """Return goal-drift trend data from the active execution graph's GoalTracker.

    Reads the drift trend from whichever AgentGraph is currently executing.
    When no execution is in progress, returns a stable sentinel so callers can
    distinguish "no data" from "drift detected".

    Returns:
        Dict containing trend data from :meth:`GoalTracker.get_drift_trend` when
        an active execution graph exists. Returns ``{"trend": "no_active_execution"}``
        when no graph is running or when the active graph has no GoalTracker.
    """
    try:
        from vetinari.orchestration.agent_graph import get_agent_graph

        graph = get_agent_graph()
        if graph is None:
            return {"trend": "no_active_execution"}

        # AgentGraph stores GoalTracker as _goal_tracker (set during execute_goal)
        goal_tracker = getattr(graph, "_goal_tracker", None)
        if goal_tracker is None:
            return {"trend": "no_active_execution"}

        trend = goal_tracker.get_drift_trend()
        return trend if isinstance(trend, dict) else {"trend": str(trend)}
    except Exception:
        logger.warning(
            "get_active_drift_trend: could not read drift trend — returning no_active_execution",
            exc_info=True,
        )
        return {"trend": "no_active_execution"}


def wire_drift_subsystem() -> None:
    """Master entry point for the startup wiring system.

    Calls startup_drift_validation() and logs the outcome. Registered with
    the startup wiring layer so the drift subsystem is always bootstrapped
    when the application starts.
    """
    is_valid = startup_drift_validation()
    if is_valid:
        logger.info("Drift subsystem wired successfully")
    else:
        logger.warning("Drift subsystem wired with schema warnings — check logs above for details")
