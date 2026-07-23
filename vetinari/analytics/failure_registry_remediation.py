"""Remediation outcome tracking for the failure registry facade."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from vetinari.boundary_guards import account_evidence_drop

logger = logging.getLogger(__name__)


class FailureRegistryRemediationMixin:
    """Remediation outcome metrics behavior for FailureRegistry."""

    def log_remediation_outcome(
        self,
        failure_mode: str,
        action_description: str,
        success: bool,
    ) -> None:
        """Log the outcome of a remediation action for trend tracking.

        Appends a record to ``~/.vetinari/remediation-outcomes.jsonl``
        (separate from the main failure registry) so that success rates
        per (failure_mode, action) pair can be computed.

        Args:
            failure_mode: The failure mode that was remediated (e.g. ``"oom"``).
            action_description: Description of the remediation action taken.
            success: Whether the remediation resolved the failure.
        """
        from vetinari.analytics.failure_registry import _get_registry_dir

        record = {
            "outcome_id": f"rem_{uuid.uuid4().hex[:12]}",
            "failure_mode": failure_mode,
            "action_description": action_description,
            "success": success,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "schema_version": 1,
        }

        path = _get_registry_dir() / "remediation-outcomes.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except OSError as exc:
            logger.error(
                "Could not write remediation outcome - record lost: %s",
                exc,
            )

        logger.info(
            "Remediation outcome logged - mode=%s action=%s success=%s",
            failure_mode,
            action_description[:80],
            success,
        )

    def get_remediation_stats(self) -> dict[tuple[str, str], dict[str, int]]:
        """Return per-(failure_mode, action) success/failure counts.

        Reads from ``~/.vetinari/remediation-outcomes.jsonl`` and aggregates
        counts.

        Returns:
            Dict mapping ``(failure_mode, action_description)`` to
            ``{"success": int, "failure": int}``.

        Raises:
            OSError: If the remediation outcome store exists but cannot be read.
        """
        from vetinari.analytics.failure_registry import _get_registry_dir

        path = _get_registry_dir() / "remediation-outcomes.jsonl"
        if not path.exists():
            return {}

        stats: dict[tuple[str, str], dict[str, int]] = {}
        try:
            with path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as exc:
                        logger.warning(
                            "Skipping malformed remediation outcome record in %s: %s",
                            path,
                            exc,
                        )
                        continue
                    key = (record.get("failure_mode", ""), record.get("action_description", ""))
                    if key not in stats:
                        stats[key] = {"success": 0, "failure": 0}
                    if record.get("success"):
                        stats[key]["success"] += 1
                    else:
                        stats[key]["failure"] += 1
        except OSError as exc:
            account_evidence_drop(
                {"path": str(path), "error": str(exc)},
                "failure_registry",
                logger=logger,
            )
            logger.warning(
                "Could not read remediation outcomes from failure_registry_remediation: %s",
                exc,
            )
            raise
        return stats

    def get_remediation_confidence(
        self,
        failure_mode: str,
        action_description: str,
    ) -> float:
        """Return confidence score for a (failure_mode, action) pair.

        Confidence is the success rate: successes / total. Returns 0.0 if
        no outcomes have been recorded for this pair.

        Args:
            failure_mode: The failure mode string.
            action_description: The remediation action description.

        Returns:
            Float between 0.0 and 1.0.
        """
        stats = self.get_remediation_stats()
        counts = stats.get((failure_mode, action_description))
        if not counts:
            return 0.0
        total = counts["success"] + counts["failure"]
        if total == 0:
            return 0.0
        return counts["success"] / total
