"""Drift Monitor — vetinari.drift.monitor  (Phase 7).

Orchestrates all drift-control components into a single entry-point that CI
and scheduled jobs call.

    DriftMonitorReport  — summary of one full drift audit cycle
    DriftMonitor — runs contract, capability, and schema checks; aggregates
                   results into a DriftMonitorReport; persists snapshots.

Usage
-----
    from vetinari.drift.monitor import get_drift_monitor

    monitor = get_drift_monitor()
    report  = monitor.run_full_audit()

    if not report.is_clean:
        for issue in report.issues:
            logger.debug(issue)
        sys.exit(1)
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .capability_auditor import get_capability_auditor
from .contract_registry import get_contract_registry
from .schema_validator import get_schema_validator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class DriftMonitorReport:
    """Aggregated result of a full drift audit."""

    timestamp: float = field(default_factory=time.time)
    contract_drifts: dict[str, dict[str, str]] = field(default_factory=dict)
    capability_drifts: list[str] = field(default_factory=list)
    schema_errors: dict[str, list[str]] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    duration_ms: float = 0.0

    def __repr__(self) -> str:
        return f"DriftMonitorReport(is_clean={self.is_clean!r}, issues={len(self.issues)}, duration_ms={self.duration_ms!r})"

    @property
    def is_clean(self) -> bool:
        """Whether the drift report found zero contract, capability, or schema issues."""
        return not self.contract_drifts and not self.capability_drifts and not self.schema_errors

    def to_dict(self) -> dict[str, Any]:
        """Serialize this DriftReport to a plain dictionary.

        Returns:
            Dictionary containing timestamp, clean flag, contract drifts,
            capability drifts, schema errors, issues, and duration.
        """
        return {
            "timestamp": self.timestamp,
            "is_clean": self.is_clean,
            "contract_drifts": self.contract_drifts,
            "capability_drifts": self.capability_drifts,
            "schema_errors": self.schema_errors,
            "issues": self.issues,
            "duration_ms": self.duration_ms,
        }

    def summary(self) -> str:
        """Summary.

        Returns:
            A human-readable one-line string: ``"Drift audit clean (N ms)"``
            when no issues were found, or ``"Drift detected: X contract
            drift(s); Y capability drift(s); Z schema error(s)"`` listing
            each non-zero category.
        """
        if self.is_clean:
            return f"Drift audit clean ({self.duration_ms:.0f} ms)"
        parts = []
        if self.contract_drifts:
            parts.append(f"{len(self.contract_drifts)} contract drift(s)")
        if self.capability_drifts:
            parts.append(f"{len(self.capability_drifts)} capability drift(s)")
        if self.schema_errors:
            parts.append(f"{len(self.schema_errors)} schema error(s)")
        return "Drift detected: " + "; ".join(parts)


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------


class DriftMonitor:
    """Orchestrates the three drift-control sub-systems.

    Singleton — use ``get_drift_monitor()``.
    """

    _instance: DriftMonitor | None = None
    _class_lock = threading.Lock()

    def __new__(cls) -> DriftMonitor:
        if cls._instance is None:
            with cls._class_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._setup()
        return cls._instance

    def _setup(self) -> None:
        self._lock = threading.RLock()
        self._registry = get_contract_registry()
        self._auditor = get_capability_auditor()
        self._validator = get_schema_validator()
        self._history: deque[DriftMonitorReport] = deque(maxlen=5000)

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------

    def bootstrap(self) -> None:
        """Seed all sub-systems with the current codebase state as the.

        baseline.  Call once at application startup (or in CI) before
        running checks.
        """
        # 1. Register Vetinari schemas
        self._validator.register_vetinari_schemas()

        # 2. Seed capability baseline from live code
        self._auditor.register_all_from_contracts()

        # 3. Seed contract snapshots from live dataclasses
        self._seed_contract_snapshots()

        logger.info("DriftMonitor bootstrapped")

    def _seed_contract_snapshots(self) -> None:
        """Register fingerprints for all known Vetinari contracts."""
        try:
            from vetinari.planning.plan_types import Plan, PlanCandidate, Subtask

            self._registry.register("Plan", Plan(goal="__seed__"))
            self._registry.register("Subtask", Subtask())
            self._registry.register("PlanCandidate", PlanCandidate())
        except Exception as exc:
            logger.warning("plan_types seed failed: %s", exc)

        try:
            from vetinari.dashboard.alerts import AlertCondition, AlertThreshold

            self._registry.register(
                "AlertThreshold",
                AlertThreshold(
                    name="__seed__",
                    metric_key="x",
                    condition=AlertCondition.GREATER_THAN,
                    threshold_value=0.0,
                ),
            )
        except Exception as exc:
            logger.warning("dashboard seed failed: %s", exc)

        try:
            from vetinari.analytics.cost import CostEntry

            self._registry.register(
                "CostEntry",
                CostEntry(
                    provider="__seed__",
                    model="__seed__",
                ),
            )
        except Exception as exc:
            logger.warning("analytics seed failed: %s", exc)

    # ------------------------------------------------------------------
    # Audit runs
    # ------------------------------------------------------------------

    def run_contract_check(self) -> dict[str, dict[str, str]]:
        """Load snapshot and compare current fingerprints.

        Returns:
            Mapping of contract name to ``{"previous": hash, "current": hash}``
            for every contract whose fingerprint has changed since the snapshot.
            Empty dict when all contracts are stable.
        """
        self._registry.load_snapshot()
        drifts = self._registry.check_drift()
        if drifts:
            logger.warning("Contract drift detected: %s", list(drifts.keys()))
        else:
            logger.info("Contract check clean (%d contracts)", len(self._registry.list_contracts()))
        return drifts

    def run_capability_check(self) -> list[str]:
        """Return text descriptions of all capability drift findings.

        Returns:
            List of human-readable drift descriptions (one per drifting agent),
            e.g. ``["[DRIFT] WORKER: undocumented=['new_cap']"]``.  Empty list
            when all agents' live capabilities match their documented baselines.
        """
        findings = self._auditor.get_drift_findings()
        texts = [str(f) for f in findings]
        if texts:
            logger.warning("Capability drift: %s", texts)
        else:
            logger.info("Capability check clean")
        return texts

    def run_schema_check(self, sample_objects: dict[str, Any] | None = None) -> dict[str, list[str]]:
        """Validate sample objects against registered schemas.

        Args:
            sample_objects: ``{ schema_name: object_to_validate }``.
                            If None, uses live instances seeded in bootstrap.

        Returns:
            Mapping of schema name to a list of validation error strings for
            every object that failed validation.  Only failing schemas appear
            as keys; empty dict means all objects passed.
        """
        if sample_objects is None:
            sample_objects = self._build_sample_objects()

        errors: dict[str, list[str]] = {}
        for name, obj in sample_objects.items():
            errs = self._validator.validate(name, obj)
            if errs:
                errors[name] = errs
                logger.warning("Schema errors for '%s': %s", name, errs)

        if not errors:
            logger.info("Schema check clean (%d objects)", len(sample_objects))
        return errors

    @staticmethod
    def _build_sample_objects() -> dict[str, Any]:
        """Build minimal valid sample objects for schema validation."""
        samples: dict[str, Any] = {}
        try:
            from vetinari.planning.plan_types import Plan

            samples["Plan"] = Plan(goal="sample plan")
        except Exception:
            logger.warning("Failed to build sample Plan object for schema validation", exc_info=True)
        try:
            from vetinari.planning.plan_types import Subtask

            samples["Subtask"] = Subtask(description="sample subtask", plan_id="p1")
        except Exception:
            logger.warning("Failed to build sample Subtask object for schema validation", exc_info=True)
        try:
            from vetinari.dashboard.log_aggregator import LogRecord

            samples["LogRecord"] = LogRecord(message="sample", level="INFO")
        except Exception:
            logger.warning("Failed to build sample LogRecord object for schema validation", exc_info=True)
        return samples

    def run_full_audit(
        self,
        sample_objects: dict[str, Any] | None = None,
        snapshot_after: bool = False,
    ) -> DriftMonitorReport:
        """Run all three checks and return a DriftReport.

        Args:
            sample_objects: Passed through to ``run_schema_check()``.
            snapshot_after: If True, save a fresh snapshot after the check
                            so the current state becomes the new baseline.

        Returns:
            The DriftReport result.
        """
        t0 = time.perf_counter()
        report = DriftMonitorReport()

        for label, runner, attr in (
            ("contract", self.run_contract_check, "contract_drifts"),
            ("capability", self.run_capability_check, "capability_drifts"),
            ("schema", lambda: self.run_schema_check(sample_objects), "schema_errors"),
        ):
            try:
                setattr(report, attr, runner())
            except Exception as exc:
                report.issues.append(f"{label} subcheck failed: {exc}")
                logger.exception("Full drift audit %s subcheck failed", label)

        # Build human-readable issue list
        for name, info in report.contract_drifts.items():
            report.issues.append(f"CONTRACT DRIFT '{name}': {info['previous'][:8]}… → {info['current'][:8]}…")
        for text in report.capability_drifts:
            report.issues.append(f"CAPABILITY DRIFT: {text}")
        for name, errs in report.schema_errors.items():
            for err in errs:
                report.issues.append(f"SCHEMA ERROR '{name}': {err}")

        report.duration_ms = (time.perf_counter() - t0) * 1000

        if snapshot_after:
            try:
                self._registry.snapshot()
            except Exception as exc:
                report.issues.append(f"snapshot failed: {exc}")
                logger.exception("Full drift audit snapshot failed")

        with self._lock:
            self._history.append(report)

        if report.is_clean:
            logger.info("Full drift audit clean in %.0f ms", report.duration_ms)
        else:
            logger.warning("Full drift audit found issues: %s", report.summary())
        return report

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def get_history(self) -> list[DriftMonitorReport]:
        """Get history.

        Returns:
            A copy of all DriftReport objects produced by previous
            ``run_full_audit()`` calls, oldest first.  Empty list if no
            audits have been run yet.
        """
        with self._lock:
            return list(self._history)

    def get_last_report(self) -> DriftMonitorReport | None:
        """Get last report.

        Returns:
            The most recent DriftReport, or None if no audits have been run.
        """
        with self._lock:
            return self._history[-1] if self._history else None

    def get_stats(self) -> dict[str, Any]:
        """Get stats.

        Returns:
            Dictionary with ``audits_run``, ``last_clean`` (bool or None),
            ``last_duration_ms`` (float or None), and nested stats dicts
            from ``contracts``, ``capabilities``, and ``schemas`` sub-systems.
        """
        with self._lock:
            last = self._history[-1] if self._history else None
        return {
            "audits_run": len(self._history),
            "last_clean": last.is_clean if last else None,
            "last_duration_ms": last.duration_ms if last else None,
            "contracts": self._registry.get_stats(),
            "capabilities": self._auditor.get_stats(),
            "schemas": self._validator.get_stats(),
        }

    def clear_history(self) -> None:
        """Clear history."""
        with self._lock:
            self._history.clear()


# ---------------------------------------------------------------------------
# Singleton helpers
# ---------------------------------------------------------------------------


def get_drift_monitor() -> DriftMonitor:
    """Return the module-level DriftMonitor singleton, creating it on first call.

    Returns:
        The shared DriftMonitor instance.
    """
    return DriftMonitor()


def reset_drift_monitor() -> None:
    """Reset drift monitor."""
    with DriftMonitor._class_lock:
        DriftMonitor._instance = None
