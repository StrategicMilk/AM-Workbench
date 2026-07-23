"""Extracted implementation helpers for improvement_log.py."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vetinari.kaizen.improvement_log import (
        ImprovementRecord,
        ImprovementStatus,
        KaizenReport,
        Observation,
    )


class ImprovementLogQueryMixin:
    """Shared method implementations for the compatibility wrapper."""

    if TYPE_CHECKING:
        _db_path: Any

    def get_improvement(self, improvement_id: str) -> ImprovementRecord | None:
        """Retrieve a single improvement by ID. See ``improvement_log_queries``.

        Returns:
            The matching ImprovementRecord, or None if the ID does not exist.
        """
        from vetinari.kaizen.improvement_log_queries import get_improvement as _get

        return _get(self, improvement_id)

    def get_improvements_by_status(self, status: ImprovementStatus) -> list[ImprovementRecord]:
        """Retrieve all improvements with a given status. See ``improvement_log_queries``.

        Returns:
            All ImprovementRecords whose status matches the given value.
        """
        from vetinari.kaizen.improvement_log_queries import get_improvements_by_status as _get

        return _get(self, status)

    def get_active_improvements(self) -> list[ImprovementRecord]:
        """Return all improvements in ACTIVE status. See ``improvement_log_queries``.

        Returns:
            ImprovementRecords currently deployed and awaiting evaluation.
        """
        from vetinari.kaizen.improvement_log_queries import get_active_improvements as _get

        return _get(self)

    def get_confirmed_improvements(self) -> list[ImprovementRecord]:
        """Return all improvements in CONFIRMED status. See ``improvement_log_queries``.

        Returns:
            ImprovementRecords that passed evaluation and are permanently adopted.
        """
        from vetinari.kaizen.improvement_log_queries import get_confirmed_improvements as _get

        return _get(self)

    def get_observations(self, improvement_id: str, days: int | None = None) -> list[Observation]:
        """Retrieve observations for an improvement. See ``improvement_log_queries``.

        Args:
            improvement_id: UUID of the improvement to retrieve observations for.
            days: If provided, restrict observations to the last N days. None returns all.

        Returns:
            Metric observations recorded against the improvement, newest first.
        """
        from vetinari.kaizen.improvement_log_queries import get_observations as _get

        return _get(self, improvement_id, days)

    def get_weekly_report(self) -> KaizenReport:
        """Generate a summary report of the kaizen system's state. See ``improvement_log_queries``.

        Returns:
            Aggregated KaizenReport covering proposed, active, confirmed, and reverted improvements.
        """
        from vetinari.kaizen.improvement_log_queries import get_weekly_report as _get

        return _get(self)

    def get_confirmed_this_week(self) -> list[ImprovementRecord]:
        """Return improvements confirmed in the last 7 days. See ``improvement_log_queries``.

        Returns:
            ImprovementRecords with confirmed_at within the past 7 days.
        """
        from vetinari.kaizen.improvement_log_queries import get_confirmed_this_week as _get

        return _get(self)

    def get_reverted_this_week(self) -> list[ImprovementRecord]:
        """Return improvements reverted in the last 7 days. See ``improvement_log_queries``.

        Returns:
            ImprovementRecords with reverted_at within the past 7 days.
        """
        from vetinari.kaizen.improvement_log_queries import get_reverted_this_week as _get

        return _get(self)

    def get_proposed_this_week(self) -> list[ImprovementRecord]:
        """Return improvements that have PROPOSED status and were created in the last 7 days.

        Both conditions must hold: status must be 'proposed' AND created_at must fall
        within the current ISO week. This is NOT all records created this week regardless
        of status — only those still in the PROPOSED state are returned.

        Returns:
            ImprovementRecords with status=PROPOSED created within the past 7 days.
        """
        from vetinari.kaizen.improvement_log_queries import get_proposed_this_week as _get

        return _get(self)

    def record_defect(
        self, category: str, agent_type: str = "", mode: str = "", task_id: str = "", confidence: float = 0.0
    ) -> None:
        """Record a defect occurrence — delegates to DefectLog.

        Args:
            category: Defect category string (e.g. "format_error", "hallucination").
            agent_type: Agent type that produced the defect (empty string if unknown).
            mode: Execution mode in which the defect occurred (empty string if unknown).
            task_id: Task identifier associated with this defect (empty string if none).
            confidence: Detector confidence in the defect classification, 0.0-1.0.
        """
        from vetinari.kaizen.defect_log import DefectLog

        DefectLog(self._db_path).record_defect(category, agent_type, mode, task_id, confidence)

    def get_weekly_defect_counts(self, weeks: int = 4) -> list[dict[str, int]]:
        """Return per-category defect counts for the last N weeks — delegates to DefectLog.

        Returns:
            One dict per week, keyed by defect category with integer occurrence counts.
        """
        from vetinari.kaizen.defect_log import DefectLog

        return DefectLog(self._db_path).get_weekly_defect_counts(weeks)

    def get_top_defect_pattern(self) -> dict[str, Any] | None:
        """Return the most frequently occurring defect category — delegates to DefectLog.

        Returns:
            Dict with category, count, and last_seen keys, or None if no defects recorded.
        """
        from vetinari.kaizen.defect_log import DefectLog

        return DefectLog(self._db_path).get_top_defect_pattern()

    def get_defect_hotspots(self, days: int = 28) -> list[dict[str, Any]]:
        """Return agent+mode combinations with highest defect rates — delegates to DefectLog.

        Returns:
            List of dicts with agent_type, mode, and defect_count, sorted by count descending.
        """
        from vetinari.kaizen.defect_log import DefectLog

        return DefectLog(self._db_path).get_defect_hotspots(days)
