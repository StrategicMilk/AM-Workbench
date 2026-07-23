"""Quality helper mixin for InspectorSkillTool."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from vetinari.skills.inspector_skill_types import ReviewIssue


class _InspectorQualityMixin:
    """Provide supplementary quality-tool helpers for InspectorSkillTool."""

    if TYPE_CHECKING:
        _quality_tool: Any

    def _get_quality_tool(self) -> Any:
        """Lazily create and cache a QualitySkillTool instance.

        The import is deferred to avoid a circular import at module load time
        and to keep QualitySkillTool as an optional dependency.

        Returns:
            A cached QualitySkillTool instance.
        """
        if self._quality_tool is None:
            from vetinari.skills.quality_skill import QualitySkillTool

            self._quality_tool = QualitySkillTool()
        return self._quality_tool

    @staticmethod
    def _merge_quality_issues(
        existing: list[ReviewIssue],
        quality_result: Any,
        dedup_field: str = "description",
    ) -> list[ReviewIssue]:
        """Merge QualitySkillTool findings into Inspector's issue list.

        Extracts QualityIssue dicts from a ToolResult, converts them to
        ReviewIssue objects, and appends only those whose deduplication key
        does not already appear in the existing list.

        Args:
            existing: Inspector's current list of ReviewIssue objects.
            quality_result: ToolResult returned by QualitySkillTool.execute().
            dedup_field: Field name used for deduplication, ``"description"``
                for code-review merges and ``"cwe"`` for security-audit merges.

        Returns:
            New list containing ``existing`` issues followed by any
            non-duplicate issues from the quality result.
        """
        if not (quality_result and quality_result.success and quality_result.output):
            return existing

        raw_issues: list[dict[str, Any]] = quality_result.output.get("issues", [])
        if not raw_issues:
            return existing

        # Build a set of existing dedup keys for O(1) look-up.
        if dedup_field == "description":
            existing_keys: set[str] = {i.description.lower() for i in existing}
        else:  # "cwe"
            existing_keys = {i.cwe.lower() for i in existing if i.cwe}

        merged = list(existing)
        for raw in raw_issues:
            # Map QualityIssue fields to ReviewIssue fields.
            description: str = raw.get("description") or raw.get("title", "")
            cwe: str = raw.get("cwe_id", "")
            owasp: str = raw.get("owasp_category", "")

            if dedup_field == "description":
                key = description.lower()
            else:
                key = cwe.lower() if cwe else description.lower()

            if key in existing_keys:
                continue

            existing_keys.add(key)
            merged.append(
                ReviewIssue(
                    severity=raw.get("severity", "low"),
                    description=description,
                    category=raw.get("category", ""),
                    cwe=cwe,
                    owasp=owasp,
                    suggestion=raw.get("suggestion", "") or "",
                ),
            )

        return merged

    @staticmethod
    def _score_to_grade(score: float) -> str:
        """Convert a numeric score to a letter grade.

        Args:
            score: Score between 0.0 and 1.0.

        Returns:
            Letter grade (A, B, C, D, or F).
        """
        if score >= 0.9:
            return "A"
        if score >= 0.8:
            return "B"
        if score >= 0.7:
            return "C"
        if score >= 0.6:
            return "D"
        return "F"
