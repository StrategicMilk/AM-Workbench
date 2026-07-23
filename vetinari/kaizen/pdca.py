"""PDCA Feedback Loop Controller — Wires Kaizen improvements to real actions.

Closes the Plan-Do-Check-Act loop by:
- **Applying** improvements when activated (Do phase)
- **Persisting** improvements when confirmed (Act phase)
- **Auto-proposing** improvements when defect trends worsen (Plan phase)

Without this module, improvements are tracked in SQLite but never actually
change system behavior.  The PDCAController registers ``ImprovementApplicator``
callables keyed by metric name; when an improvement targeting that metric is
activated, the applicator is invoked.  On confirmation the applied change is
written to a durable JSON overrides file so it survives restarts.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vetinari.kaizen.pdca_cycle import PDCACycleMixin

if TYPE_CHECKING:
    from vetinari.kaizen.knowledge_lint import KnowledgeLintReport

from vetinari.constants import get_user_dir
from vetinari.exceptions import ExecutionError
from vetinari.kaizen.defect_trends import (
    DefectTrendAnalyzer,
)
from vetinari.kaizen.improvement_log import (
    ImprovementLog,
    ImprovementRecord,
    ImprovementStatus,
)
from vetinari.kaizen.pdca_applicators import (
    CatalogFreshnessApplicator,
    CatalogUpdateProposal,
    ImprovementApplicator,
    KaizenApplyReceipt,
    ThresholdApplicator,
    ThresholdOverride,
    _safe_receipt_changes,
)
from vetinari.learning.atomic_writers import write_json_atomic
from vetinari.privacy.envelope import PRIVACY_ENVELOPE_KEY, wrap_for_persistence
from vetinari.security.redaction import redact_text, redact_value

logger = logging.getLogger(__name__)


def _get_default_overrides_path() -> Path:
    """Return the default overrides path, using get_user_dir() for testability."""
    return get_user_dir() / "kaizen_overrides.json"


# ── PDCA Controller ──────────────────────────────────────────────────────────


class PDCAController(PDCACycleMixin):
    """Orchestrates the full PDCA feedback loop for kaizen improvements.

    Bridges the gap between ImprovementLog (data store) and real system
    changes.  Registers applicators per metric, invokes them on activation,
    persists changes on confirmation, and auto-proposes improvements when
    defect trends worsen.

    Args:
        improvement_log: The kaizen ImprovementLog instance.
        overrides_path: Path to the JSON file for persisting confirmed overrides.
    """

    def __init__(
        self,
        improvement_log: ImprovementLog,
        overrides_path: Path | str | None = None,
        receipt_path: Path | str | None = None,
    ) -> None:
        self._log = improvement_log
        self._overrides_path = Path(overrides_path) if overrides_path else _get_default_overrides_path()
        self._receipt_path = (
            Path(receipt_path) if receipt_path else self._overrides_path.with_name("kaizen_apply_receipts.jsonl")
        )
        self._applicators: dict[str, ImprovementApplicator] = {}
        self._applied: dict[str, dict[str, Any]] = {}
        self._receipt_lock = threading.Lock()
        self._trend_analyzer = DefectTrendAnalyzer()

    def register_applicator(self, metric: str, applicator: ImprovementApplicator) -> None:
        """Register an applicator for a given metric name.

        Args:
            metric: The metric name (e.g. 'quality', 'latency', 'throughput').
            applicator: Callable that applies improvements for this metric.
        """
        self._applicators[metric] = applicator
        logger.info("Registered improvement applicator for metric=%s", metric)

    # ── Do phase: activate and apply ─────────────────────────────────────

    def activate_and_apply(
        self,
        improvement_id: str,
        *,
        require_registered_applicator: bool = False,
    ) -> dict[str, Any]:
        """Activate an improvement and apply it to the running system.

        Calls ``ImprovementLog.activate()`` to transition the status, then
        looks up the registered applicator for the improvement's metric and
        invokes it.  If no applicator is registered, the improvement is
        still activated but nothing is applied.

        Args:
            improvement_id: The improvement to activate.
            require_registered_applicator: Raise when the improvement's
                metric has no registered applicator instead of recording a
                no-op activation.

        Returns:
            Dict describing what was applied (empty if no applicator matched).

        Raises:
            ValueError: If the improvement does not exist or is not in
                PROPOSED status.
        """
        # Transition status in the log — ImprovementLog.activate() emits
        # KaizenImprovementActive internally, so we must NOT emit again here.
        pre_activation_record = self._log.get_improvement(improvement_id)
        if pre_activation_record is None:
            raise ExecutionError(f"Improvement not found: {improvement_id}")
        if require_registered_applicator and pre_activation_record.metric not in self._applicators:
            raise ExecutionError(
                f"No registered applicator for metric={pre_activation_record.metric!r}; "
                f"refusing to activate improvement {improvement_id}"
            )

        self._log.activate(improvement_id)

        record = self._log.get_improvement(improvement_id)
        if record is None:
            raise ExecutionError(f"Improvement not found after activation: {improvement_id}")

        applicator = self._applicators.get(record.metric)
        if applicator is None:
            logger.info(
                "No applicator registered for metric=%s; improvement %s activated but not applied",
                record.metric,
                improvement_id,
            )
            return {}

        try:
            changes = applicator(record)
        except Exception:
            # Roll back to PROPOSED if the applicator fails — leaving the
            # improvement ACTIVE with no actual change applied would make the
            # observation window meaningless and could corrupt the baseline.
            logger.error(
                "Applicator for metric=%s raised while applying improvement %s — "
                "reverting to PROPOSED so the improvement can be retried",
                record.metric,
                improvement_id,
                exc_info=True,
            )
            self._log.revert_to_proposed(improvement_id)
            self._write_apply_receipt(
                KaizenApplyReceipt(
                    improvement_id=improvement_id,
                    metric=record.metric,
                    status="apply_failed_reverted_to_proposed",
                    evidence="applicator raised; improvement status reverted before retry",
                )
            )
            raise

        self._applied[improvement_id] = changes
        self._write_apply_receipt(
            KaizenApplyReceipt(
                improvement_id=improvement_id,
                metric=record.metric,
                status="applied",
                evidence="registered applicator completed",
                changes=_safe_receipt_changes(dict(changes)),
            )
        )
        logger.info(
            "Improvement applied: id=%s, metric=%s, changes=%s",
            improvement_id,
            record.metric,
            _safe_receipt_changes(dict(changes)),
        )
        return changes

    # ── Act phase: confirm and persist ───────────────────────────────────

    def confirm_and_persist(self, improvement_id: str) -> None:
        """Persist an improvement's changes after successful evaluation.

        Writes the applied changes to the overrides JSON file so they
        survive restarts.  Should be called after ``ImprovementLog.evaluate()``
        returns CONFIRMED.

        Args:
            improvement_id: The improvement to persist.
        """
        record = self._log.get_improvement(improvement_id)
        if record is None:
            logger.warning("Cannot persist unknown improvement: %s", improvement_id)
            return

        if record.status != ImprovementStatus.CONFIRMED:
            logger.warning(
                "Cannot persist improvement %s — status is %s, expected CONFIRMED",
                improvement_id,
                record.status.value,
            )
            return

        changes = self._applied.get(improvement_id, {})
        self._write_override(improvement_id, record, changes)

        # Mark the applicator override as confirmed
        applicator = self._applicators.get(record.metric)
        if isinstance(applicator, ThresholdApplicator):
            applicator.confirm_override(improvement_id)

        logger.info(
            "Improvement persisted: id=%s, metric=%s",
            improvement_id,
            record.metric,
        )

    def _write_override(
        self,
        improvement_id: str,
        record: ImprovementRecord,
        changes: dict[str, Any],
    ) -> None:
        """Append an override entry to the JSON overrides file.

        Args:
            improvement_id: The improvement ID.
            record: The improvement record.
            changes: The changes dict returned by the applicator.
        """
        self._overrides_path.parent.mkdir(parents=True, exist_ok=True)
        overrides: list[dict[str, Any]] = []
        if self._overrides_path.exists():
            try:
                raw = self._overrides_path.read_text(encoding="utf-8")
                overrides = json.loads(raw) if raw.strip() else []
            except (json.JSONDecodeError, OSError):
                logger.warning(
                    "Could not read overrides file %s — starting fresh",
                    self._overrides_path,
                )

        overrides.append({
            "improvement_id": improvement_id,
            "metric": redact_text(record.metric),
            "hypothesis": redact_text(record.hypothesis),
            "baseline_value": record.baseline_value,
            "target_value": record.target_value,
            "actual_value": record.actual_value,
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
            "changes": redact_value(changes),
            PRIVACY_ENVELOPE_KEY: wrap_for_persistence(
                {"improvement_id": improvement_id, "metric": record.metric},
                privacy_class="operational",
                source="kaizen.pdca.override",
                redaction_applied=True,
            )[PRIVACY_ENVELOPE_KEY],
        })
        write_json_atomic(self._overrides_path, overrides)

    def load_persisted_overrides(self) -> list[dict[str, Any]]:
        """Load previously persisted overrides from the JSON file.

        Returns:
            List of override dicts, or empty list if file doesn't exist.
        """
        if not self._overrides_path.exists():
            return []
        try:
            raw = self._overrides_path.read_text(encoding="utf-8")
            return json.loads(raw) if raw.strip() else []
        except (json.JSONDecodeError, OSError):
            logger.warning("Could not read overrides file: %s", self._overrides_path)
            return []

    # ── Plan phase: trend analysis → auto-propose ────────────────────────

    # ── Knowledge lint ───────────────────────────────────────────────────

    def _write_apply_receipt(self, receipt: KaizenApplyReceipt) -> None:
        self._receipt_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(asdict(receipt), separators=(",", ":"), sort_keys=True) + "\n"
        with self._receipt_lock, self._receipt_path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
            fh.flush()

    def load_apply_receipts(self) -> list[dict[str, Any]]:
        """Load durable apply/revert receipts recorded by this controller.

        Returns:
            List of receipt dictionaries in write order.

        Raises:
            FileNotFoundError: If the durable receipt log has not been created.
        """
        if not self._receipt_path.exists():
            raise FileNotFoundError(f"Kaizen apply receipt log is missing: {self._receipt_path}")
        return [
            json.loads(line) for line in self._receipt_path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]

    def run_check_phase(self) -> list[str]:
        """Evaluate active improvements and record rollback evidence for failures.

        Returns:
            Improvement IDs confirmed and persisted during this check.
        """
        active = self._log.get_active_improvements()
        confirmed_ids: list[str] = []
        now_utc = datetime.now(timezone.utc)
        for improvement in active:
            observations = self._log.get_observations(improvement.id)
            if not observations:
                if improvement.applied_at is not None:
                    window_expires = improvement.applied_at + improvement.observation_window
                    if now_utc > window_expires:
                        logger.warning(
                            "Improvement %s stuck in ACTIVE: observation window expired "
                            "with no observations - reverting to PROPOSED for retry",
                            improvement.id,
                        )
                        try:
                            self._log.revert_to_proposed(improvement.id)
                            self._write_apply_receipt(
                                KaizenApplyReceipt(
                                    improvement_id=improvement.id,
                                    metric=improvement.metric,
                                    status="observation_window_expired_reverted_to_proposed",
                                    evidence="no observations before observation window expired",
                                )
                            )
                        except Exception:
                            logger.error(
                                "Failed to revert stuck improvement %s to PROPOSED",
                                improvement.id,
                                exc_info=True,
                            )
                continue
            result = self._log.evaluate(improvement.id)
            if result == ImprovementStatus.CONFIRMED:
                self.confirm_and_persist(improvement.id)
                confirmed_ids.append(improvement.id)
            elif result == ImprovementStatus.FAILED:
                applicator = self._applicators.get(improvement.metric)
                rollback_evidence = "no registered rollback handler"
                receipt_status = "failed_no_rollback_handler"
                if isinstance(applicator, ThresholdApplicator):
                    reverted_to = applicator.revert_override(improvement.id)
                    rollback_evidence = f"threshold reverted to {reverted_to!r}"
                    receipt_status = "failed_reverted"
                self._write_apply_receipt(
                    KaizenApplyReceipt(
                        improvement_id=improvement.id,
                        metric=improvement.metric,
                        status=receipt_status,
                        evidence=rollback_evidence,
                        changes=_safe_receipt_changes(dict(self._applied.get(improvement.id, {}))),
                    )
                )
                logger.info(
                    "Improvement %s failed evaluation - changes reverted",
                    improvement.id,
                )

        from vetinari.kaizen.knowledge_compactor import run_compaction_step

        run_compaction_step()
        return confirmed_ids

    def knowledge_lint(self) -> KnowledgeLintReport:
        """Run knowledge lint checks on all memory entries.

        Returns:
            KnowledgeLintReport with findings from contradiction, stale,
            orphaned, and vocabulary drift checks.

        Raises:
            Exception: If memory store is unavailable or linter fails.
        """
        from vetinari.kaizen.knowledge_lint import KnowledgeLinter, propose_lint_findings
        from vetinari.memory.unified import UnifiedMemoryStore

        try:
            entries = UnifiedMemoryStore().search("", limit=10_000)
        except Exception:
            logger.error(
                "Knowledge lint: backing store unavailable — cannot run lint checks",
                exc_info=True,
            )
            raise

        # Linter failures propagate — callers must not silently receive an
        # empty report when the linter itself is broken.
        report = KnowledgeLinter().lint_all(entries)

        try:
            propose_lint_findings(self._log, report)
        except Exception:
            logger.warning(
                "Knowledge lint: propose_lint_findings failed — findings not proposed to improvement log",
                exc_info=True,
            )

        return report

    # ── Full PDCA cycle convenience ──────────────────────────────────────


__all__ = [
    "CatalogFreshnessApplicator",
    "CatalogUpdateProposal",
    "ImprovementApplicator",
    "KaizenApplyReceipt",
    "PDCAController",
    "ThresholdApplicator",
    "ThresholdOverride",
    "_safe_receipt_changes",
]
