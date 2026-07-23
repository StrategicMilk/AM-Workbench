"""PDCA applicator value objects and built-in applicators.

Extracted from ``pdca.py`` to keep that module under the 500-LOC target.
This module owns:

* ``ImprovementApplicator`` — callable type alias
* ``ThresholdOverride`` / ``ThresholdApplicator`` — runtime threshold adjustment
* ``KaizenApplyReceipt`` / ``_safe_receipt_changes`` — durable apply receipts
* ``CatalogUpdateProposal`` / ``CatalogFreshnessApplicator`` — catalog-freshness
  applicator for the ``catalog_freshness`` PDCA metric
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vetinari.kaizen.improvement_log import ImprovementRecord

if TYPE_CHECKING:
    from vetinari.kaizen.catalog_freshness import CatalogFreshnessReport

logger = logging.getLogger(__name__)

# Regex constants used by _safe_receipt_changes to redact sensitive data.
_RECEIPT_SENSITIVE_KEY_RE = re.compile(r"(?i)(token|secret|password|api[_-]?key|url|path)")
_RECEIPT_SENSITIVE_VALUE_RE = re.compile(r"(?i)(://|[A-Za-z]:[\\/]|/[A-Za-z0-9_.-])")


ImprovementApplicator = Callable[[ImprovementRecord], dict[str, Any]]  # applies an improvement, returns changes

# ── Built-in: Threshold applicator ───────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ThresholdOverride:
    """A runtime threshold override applied by the PDCA loop."""

    metric: str
    previous_value: float
    new_value: float
    improvement_id: str
    applied_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    confirmed: bool = False

    def __repr__(self) -> str:
        """Compact representation showing metric, value change, and confirmation state."""
        return (
            f"ThresholdOverride(metric={self.metric!r}, "
            f"{self.previous_value}->{self.new_value}, "
            f"id={self.improvement_id!r}, confirmed={self.confirmed})"
        )


@dataclass(frozen=True, slots=True)
class KaizenApplyReceipt:
    """Durable receipt for a Kaizen apply or rollback decision."""

    improvement_id: str
    metric: str
    status: str
    evidence: str
    changes: dict[str, Any] = field(default_factory=dict)
    recorded_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __repr__(self) -> str:
        return (
            "KaizenApplyReceipt("
            f"improvement_id={self.improvement_id!r}, metric={self.metric!r}, "
            f"status={self.status!r})"
        )


def _safe_receipt_changes(changes: dict[str, Any]) -> dict[str, Any]:
    """Redact sensitive keys/values before persisting apply receipts.

    Args:
        changes: Raw changes dict from an applicator.

    Returns:
        Copy of ``changes`` with tokens, secrets, paths, and URLs redacted.
    """
    safe: dict[str, Any] = {}
    for key, value in changes.items():
        safe[str(key)] = _safe_receipt_value(str(key), value)
    return safe


def _safe_receipt_value(key: str, value: Any) -> Any:
    if _RECEIPT_SENSITIVE_KEY_RE.search(key):
        return "<redacted>"
    if isinstance(value, str):
        return "<redacted>" if _RECEIPT_SENSITIVE_VALUE_RE.search(value) else value
    if isinstance(value, dict):
        return {str(item_key): _safe_receipt_value(str(item_key), item_value) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_safe_receipt_value("", item) for item in value]
    if isinstance(value, tuple):
        return tuple(_safe_receipt_value("", item) for item in value)
    if isinstance(value, int | float | bool) or value is None:
        return value
    return repr(value)[:120]


class ThresholdApplicator:
    """Applies improvements by adjusting runtime threshold values.

    Manages a registry of named thresholds (e.g. ``quality``, ``latency``,
    ``throughput``) with current values.  When an improvement targeting one
    of these metrics is activated, the threshold is adjusted toward the
    target value.

    Args:
        initial_thresholds: Starting threshold values keyed by metric name.
    """

    def __init__(self, initial_thresholds: dict[str, float] | None = None) -> None:
        self._thresholds: dict[str, float] = dict(initial_thresholds or {})
        self._overrides: list[ThresholdOverride] = []
        self._lock = threading.Lock()

    @property
    def thresholds(self) -> dict[str, float]:
        """Current threshold values (read-only copy)."""
        with self._lock:
            return dict(self._thresholds)

    @property
    def overrides(self) -> list[ThresholdOverride]:
        """History of all threshold overrides applied."""
        return list(self._overrides)

    def get_threshold(self, metric: str) -> float | None:
        """Get the current value for a named threshold.

        Args:
            metric: The threshold metric name.

        Returns:
            Current value, or None if the metric is not registered.
        """
        with self._lock:
            return self._thresholds.get(metric)

    def __call__(self, record: ImprovementRecord) -> dict[str, Any]:
        """Apply a threshold adjustment based on the improvement's target.

        Moves the threshold for ``record.metric`` to ``record.target_value``.
        If the metric is not registered, the override is still recorded
        (the metric is created).

        Args:
            record: The improvement being activated.

        Returns:
            Dict describing the change: metric, previous, new, improvement_id.
        """
        with self._lock:
            previous = self._thresholds.get(record.metric, record.baseline_value)
            self._thresholds[record.metric] = record.target_value

            override = ThresholdOverride(
                metric=record.metric,
                previous_value=previous,
                new_value=record.target_value,
                improvement_id=record.id,
            )
            self._overrides.append(override)

        logger.info(
            "Threshold applied: metric=%s, %s -> %s (improvement=%s)",
            record.metric,
            previous,
            record.target_value,
            record.id,
        )
        return {
            "metric": record.metric,
            "previous": previous,
            "new": record.target_value,
            "improvement_id": record.id,
        }

    def confirm_override(self, improvement_id: str) -> None:
        """Mark an override as confirmed (permanently applied).

        Args:
            improvement_id: The improvement whose override to confirm.
        """
        with self._lock:
            for index, override in enumerate(self._overrides):
                if override.improvement_id == improvement_id:
                    self._overrides[index] = replace(override, confirmed=True)

    def revert_override(self, improvement_id: str) -> float | None:
        """Revert a threshold to its pre-override value.

        Args:
            improvement_id: The improvement whose override to revert.

        Returns:
            The reverted-to value, or None if no override was found.
        """
        with self._lock:
            for override in reversed(self._overrides):
                if override.improvement_id == improvement_id and not override.confirmed:
                    self._thresholds[override.metric] = override.previous_value
                    logger.info(
                        "Threshold reverted: metric=%s, %s -> %s (improvement=%s)",
                        override.metric,
                        override.new_value,
                        override.previous_value,
                        improvement_id,
                    )
                    return override.previous_value
        return None


# ── Built-in: Catalog freshness applicator ───────────────────────────────────


@dataclass(frozen=True, slots=True)
class CatalogUpdateProposal:
    """Proposed catalog update produced by CatalogFreshnessApplicator.

    Lists the stale and missing-source entries discovered by a
    ``CatalogFreshnessReport`` so an operator can prioritise which catalog
    entries need refreshing first.
    """

    stale_entries: tuple[str, ...]  # entries whose verified_on date is past the fail threshold
    missing_source_entries: tuple[str, ...]  # entries that have no source_url or unfetched source
    improvement_id: str
    generated_at: datetime

    @property
    def is_empty(self) -> bool:
        """Return True when neither stale nor missing-source entries were found."""
        return not self.stale_entries and not self.missing_source_entries

    def __repr__(self) -> str:
        """Compact representation showing counts of flagged entries."""
        return (
            f"CatalogUpdateProposal(stale={len(self.stale_entries)}, "
            f"missing_sources={len(self.missing_source_entries)}, "
            f"id={self.improvement_id!r})"
        )


class CatalogFreshnessApplicator:
    """Consumes a CatalogFreshnessReport and produces a CatalogUpdateProposal.

    Implements ``ImprovementApplicator`` so it can be registered with
    ``PDCAController`` under the ``catalog_freshness`` metric.  When the PDCA
    loop activates an improvement for that metric, this applicator inspects
    the most recent ``CatalogFreshnessReport`` and returns a proposal listing
    which catalog rows need attention.

    Wire into the PDCA loop via::

        controller.register_applicator(
            "catalog_freshness",
            CatalogFreshnessApplicator(catalog_root),
        )

    Args:
        catalog_root: Repository root directory containing the ``config/``
            tree.  Passed to ``evaluate_catalog_freshness`` when no explicit
            report is supplied.
    """

    # Metric name used when registering with PDCAController
    METRIC = "catalog_freshness"

    def __init__(self, catalog_root: Path | str) -> None:
        self._catalog_root = Path(catalog_root)
        self._last_proposal: CatalogUpdateProposal | None = None

    @property
    def last_proposal(self) -> CatalogUpdateProposal | None:
        """Most recent proposal produced by this applicator, or None."""
        return self._last_proposal

    def apply(self, report: CatalogFreshnessReport, improvement_id: str) -> CatalogUpdateProposal:
        """Produce a CatalogUpdateProposal from a CatalogFreshnessReport.

        Combines the stale entries (fail threshold) with entries missing
        source URLs into a single ranked proposal.

        Args:
            report: The freshness report to consume.
            improvement_id: The PDCA improvement ID triggering this application.

        Returns:
            CatalogUpdateProposal listing stale and missing-source entries.
        """
        proposal = CatalogUpdateProposal(
            stale_entries=report.entries_stale_fail,
            missing_source_entries=report.entries_missing_sources,
            improvement_id=improvement_id,
            generated_at=datetime.now(timezone.utc),
        )
        self._last_proposal = proposal
        logger.info(
            "CatalogFreshnessApplicator: improvement=%s stale=%d missing_sources=%d",
            improvement_id,
            len(proposal.stale_entries),
            len(proposal.missing_source_entries),
        )
        return proposal

    def __call__(self, record: ImprovementRecord) -> dict[str, Any]:
        """ImprovementApplicator protocol: evaluate freshness and return proposal dict.

        Called by ``PDCAController.activate_and_apply`` when an improvement
        targeting ``catalog_freshness`` is activated.  Runs
        ``evaluate_catalog_freshness`` against the configured catalog root and
        produces a ``CatalogUpdateProposal``.

        Args:
            record: The improvement record being activated.

        Returns:
            Dict with ``stale_count``, ``missing_source_count``,
            ``stale_entries``, and ``missing_source_entries`` keys.
        """
        from vetinari.kaizen.catalog_freshness import evaluate_catalog_freshness

        report = evaluate_catalog_freshness(self._catalog_root)
        proposal = self.apply(report, record.id)
        return {
            "stale_count": len(proposal.stale_entries),
            "missing_source_count": len(proposal.missing_source_entries),
            "stale_entries": list(proposal.stale_entries),
            "missing_source_entries": list(proposal.missing_source_entries),
        }
