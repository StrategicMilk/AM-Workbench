"""Runtime selection for contextual Workbench tool guides."""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from vetinari.constants import PROJECT_ROOT
from vetinari.context.window_manager import count_tokens
from vetinari.workbench.tool_guides.contracts import (
    ActiveToolContext,
    SelectedToolGuide,
    ToolGuide,
    ToolGuideDiagnostic,
    ToolGuideError,
    ToolGuideSelection,
    ToolGuideStatus,
    validate_tool_guide_catalog_payload,
)

logger = logging.getLogger(__name__)


_CATALOG_PATH = PROJECT_ROOT / "config" / "workbench" / "tool_guides.yaml"
_CATALOG_LOCK = threading.Lock()
_CATALOG_CACHE: tuple[ToolGuide, ...] | None = None
DEFAULT_SELECTION_TOKEN_BUDGET = 360


def _load_uncached(path: Path) -> tuple[ToolGuide, ...]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ToolGuideError(f"tool guide catalog unavailable: {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ToolGuideError("tool guide catalog must be a mapping")
    return validate_tool_guide_catalog_payload(payload)


def _load_default_token_budget(path: Path) -> int:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ToolGuideError(f"tool guide catalog unavailable: {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ToolGuideError("tool guide catalog must be a mapping")
    raw_budget = payload.get("default_token_budget", DEFAULT_SELECTION_TOKEN_BUDGET)
    try:
        budget = int(raw_budget)
    except (TypeError, ValueError) as exc:
        raise ToolGuideError("default_token_budget must be a positive integer") from exc
    if budget < 1:
        raise ToolGuideError("default_token_budget must be positive")
    return budget


def load_tool_guide_catalog(path: Path | str | None = None) -> tuple[ToolGuide, ...]:
    """Load the curated tool guide catalog, caching only the default path.

    Returns:
        Resolved tool guide catalog value.
    """
    global _CATALOG_CACHE
    if path is not None:
        return _load_uncached(Path(path))
    with _CATALOG_LOCK:
        if _CATALOG_CACHE is None:
            _CATALOG_CACHE = _load_uncached(_CATALOG_PATH)
        return _CATALOG_CACHE


def reset_tool_guide_catalog_for_test() -> None:
    """Clear the module-level catalog cache for deterministic tests."""
    global _CATALOG_CACHE
    with _CATALOG_LOCK:
        _CATALOG_CACHE = None


class ToolGuideRegistry:
    """Select bounded, attributed guide text for caller-supplied active tools."""

    def __init__(self, *, catalog_path: Path | str | None = None, guides: tuple[ToolGuide, ...] | None = None) -> None:
        self._catalog_path = Path(catalog_path) if catalog_path is not None else None
        self._guides = guides

    def _load(self) -> tuple[ToolGuide, ...]:
        if self._guides is not None:
            return self._guides
        return load_tool_guide_catalog(self._catalog_path)

    def _selection_default_token_budget(self) -> int:
        if self._guides is not None:
            return DEFAULT_SELECTION_TOKEN_BUDGET
        return _load_default_token_budget(self._catalog_path or _CATALOG_PATH)

    def catalog_metadata(self) -> list[dict[str, Any]]:
        """Return guide metadata without prompt-sized guide text."""
        return [guide.to_dict(include_guidance=False) for guide in self._load()]

    def validate_catalog_payload(self, payload: Mapping[str, Any]) -> list[ToolGuideDiagnostic]:
        """Return validation diagnostics for a candidate catalog payload.

        Returns:
            Validation outcome for catalog payload.
        """
        try:
            validate_tool_guide_catalog_payload(payload)
        except ToolGuideError as exc:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return [
                ToolGuideDiagnostic(
                    status=ToolGuideStatus.BLOCKED,
                    message="candidate catalog failed validation",
                    detail=str(exc),
                )
            ]
        return []

    def select(
        self,
        active_tools: tuple[ActiveToolContext, ...],
        *,
        now_utc: datetime | None = None,
        token_budget: int | None = None,
    ) -> ToolGuideSelection:
        """Return selected guide text and typed diagnostics for active tools.

        Returns:
            ToolGuideSelection value produced by select().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        budget_result = self._selection_budget(token_budget)
        if isinstance(budget_result, ToolGuideSelection):
            return budget_result
        budget = budget_result
        if budget < 1:
            raise ToolGuideError("token_budget must be positive")
        now = now_utc or datetime.now(UTC)
        active_by_id = {tool.tool_id: tool for tool in active_tools}
        guides_result = self._selection_guides(budget)
        if isinstance(guides_result, ToolGuideSelection):
            return guides_result

        selected: list[SelectedToolGuide] = []
        diagnostics: list[ToolGuideDiagnostic] = []
        bounded_parts: list[str] = []
        total_tokens = 0

        for guide in guides_result:
            total_tokens += self._append_guide_selection(
                guide,
                active_by_id,
                now,
                budget,
                total_tokens,
                selected,
                diagnostics,
                bounded_parts,
            )

        return ToolGuideSelection(
            selected_guides=tuple(selected),
            diagnostics=tuple(diagnostics),
            bounded_text="\n\n".join(bounded_parts),
            total_token_count=total_tokens,
            token_budget=budget,
        )

    def _selection_budget(self, token_budget: int | None) -> int | ToolGuideSelection:
        try:
            return int(token_budget) if token_budget is not None else self._selection_default_token_budget()
        except ToolGuideError as exc:
            logger.warning("Tool guide catalog unavailable; using fallback token budget", exc_info=True)
            return _blocked_selection(str(exc), DEFAULT_SELECTION_TOKEN_BUDGET)

    def _selection_guides(self, budget: int) -> tuple[ToolGuide, ...] | ToolGuideSelection:
        try:
            return self._load()
        except ToolGuideError as exc:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return _blocked_selection(str(exc), budget)

    def _append_guide_selection(
        self,
        guide: ToolGuide,
        active_by_id: dict[str, ActiveToolContext],
        now: datetime,
        budget: int,
        total_tokens: int,
        selected: list[SelectedToolGuide],
        diagnostics: list[ToolGuideDiagnostic],
        bounded_parts: list[str],
    ) -> int:
        active = self._matching_active_tool(guide, active_by_id)
        if active is None:
            diagnostics.append(
                _selection_diagnostic(guide, ToolGuideStatus.INACTIVE_TOOL, "guide tool identity is not active")
            )
            return 0
        narrowing_status = self._narrowing_status(guide, active)
        if narrowing_status is not None:
            diagnostics.append(
                _selection_diagnostic(
                    guide,
                    narrowing_status,
                    f"guide does not match active tool context: {narrowing_status.value}",
                )
            )
            return 0
        if datetime.fromisoformat(guide.stale_after).date() < now.date():
            diagnostics.append(
                _selection_diagnostic(
                    guide,
                    ToolGuideStatus.STALE_GUIDE,
                    "guide stale_after is before selection time",
                    detail=guide.stale_after,
                )
            )
            return 0
        attribution = f"{guide.guide_id}@{guide.version} via {guide.provenance_refs[0]}"
        text = self._format_selected_text(guide, attribution)
        token_count = count_tokens(text)
        if token_count > guide.token_budget or total_tokens + token_count > budget:
            diagnostics.append(
                _selection_diagnostic(
                    guide,
                    ToolGuideStatus.BUDGET_EXCEEDED,
                    "guide text exceeded token budget and was excluded",
                    detail=f"guide_tokens={token_count}; guide_budget={guide.token_budget}; selection_budget={budget}",
                )
            )
            return 0
        bounded_parts.append(text)
        selected.append(_selected_tool_guide(guide, text, token_count, attribution))
        return token_count

    @staticmethod
    def _matching_active_tool(guide: ToolGuide, active_by_id: dict[str, ActiveToolContext]) -> ActiveToolContext | None:
        for tool_id in guide.applicability.tool_ids:
            active = active_by_id.get(tool_id)
            if active is not None:
                return active
        return None

    @staticmethod
    def _narrowing_status(guide: ToolGuide, active: ActiveToolContext) -> ToolGuideStatus | None:
        applicability = guide.applicability
        if applicability.surface_kinds and active.surface_kind not in applicability.surface_kinds:
            return ToolGuideStatus.INACTIVE_TOOL
        if applicability.workflow_action_ids and active.workflow_action_id not in applicability.workflow_action_ids:
            return ToolGuideStatus.INACTIVE_TOOL
        if applicability.capability_pack_ids and not set(applicability.capability_pack_ids).intersection(
            active.capability_pack_ids
        ):
            return ToolGuideStatus.INACTIVE_TOOL
        if applicability.capability_fingerprints and not set(applicability.capability_fingerprints).intersection(
            active.capability_fingerprints
        ):
            return ToolGuideStatus.FINGERPRINT_MISMATCH
        return None

    @staticmethod
    def _format_selected_text(guide: ToolGuide, attribution: str) -> str:
        safety = " ".join(guide.safety_notes)
        example = f" Example: {guide.examples[0]}" if guide.examples else ""
        return f"[{attribution}]\n{guide.guidance}\nSafety: {safety}{example}"


def _blocked_selection(detail: str, token_budget: int) -> ToolGuideSelection:
    return ToolGuideSelection(
        diagnostics=(
            ToolGuideDiagnostic(
                status=ToolGuideStatus.BLOCKED,
                message="tool guide catalog unavailable",
                detail=detail,
            ),
        ),
        token_budget=token_budget,
    )


def _selection_diagnostic(
    guide: ToolGuide,
    status: ToolGuideStatus,
    message: str,
    *,
    detail: str = "",
) -> ToolGuideDiagnostic:
    return ToolGuideDiagnostic(
        status=status,
        guide_id=guide.guide_id,
        message=message,
        detail=detail,
        provenance_refs=guide.provenance_refs,
    )


def _selected_tool_guide(
    guide: ToolGuide,
    text: str,
    token_count: int,
    attribution: str,
) -> SelectedToolGuide:
    return SelectedToolGuide(
        guide_id=guide.guide_id,
        version=guide.version,
        text=text,
        token_count=token_count,
        attribution=attribution,
        provenance_refs=guide.provenance_refs,
        safety_notes=guide.safety_notes,
        examples=guide.examples,
    )


__all__ = [
    "DEFAULT_SELECTION_TOKEN_BUDGET",
    "ToolGuideRegistry",
    "load_tool_guide_catalog",
    "reset_tool_guide_catalog_for_test",
]
