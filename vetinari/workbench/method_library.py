"""Measured Workbench method-card library over the metadata spine.

This module implements ADR-0125 as a read-only derived view over Pack AA's
Workbench spine. It has no import-time I/O. First use loads the method catalog
from ``config/workbench/method_cards.yaml`` under a lock, then reads
``WorkbenchRun`` and ``EvalResult`` rows from the spine to attach measured
evidence to each card.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import yaml

from vetinari.constants import PROJECT_ROOT
from vetinari.workbench.evals import EvalResult, EvalScore
from vetinari.workbench.measurement.measured_delta import MeasuredDeltaError, summarize_measured_deltas
from vetinari.workbench.metadata_spine import WorkbenchSpine, WorkbenchSpineCorrupt, get_workbench_spine
from vetinari.workbench.method_library_prompt_promotion import (
    MethodPromotionRejected,
    record_prompt_method_evidence,
)
from vetinari.workbench.method_library_records import (
    MIN_EVALUATIONS_FOR_ESTIMATE,
    MeasuredDelta,
    MethodCard,
    MethodEvidenceRef,
    MethodKind,
    MethodLibraryError,
    MethodLibraryProjectIdRejected,
    PromotionStatus,
)
from vetinari.workbench.proposals import WorkbenchProposal
from vetinari.workbench.runs import RunMetric, WorkbenchRun

logger = logging.getLogger(__name__)


_METHOD_CATALOG_CONFIG_PATH: Path = PROJECT_ROOT / "config" / "workbench" / "method_cards.yaml"
_METHOD_CATALOG_LOCK: threading.Lock = threading.Lock()
_METHOD_CATALOG_CACHE: dict[str, dict[str, Any]] = {}
_METHOD_KIND_METRIC_NAME = "method_kind"
_INSUFFICIENT_DATA_FLOOR: float = 0.5


def _canonicalize_project_id(value: str | None) -> str:
    """Return the shared spine project id or fail closed with this module's error."""
    from vetinari.workbench.spine import WorkbenchProjectIdRejected, validate_project_id

    try:
        return validate_project_id(value)
    except WorkbenchProjectIdRejected as exc:
        raise MethodLibraryProjectIdRejected(value) from exc


def _copy_catalog_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "display_label": str(row["display_label"]),
        "description": str(row["description"]),
        "when_to_use": list(row["when_to_use"]),
        "when_not_to_use": list(row["when_not_to_use"]),
        "expected_cost": str(row["expected_cost"]),
        "known_failure_modes": list(row["known_failure_modes"]),
        "compatible_task_profiles": list(row["compatible_task_profiles"]),
    }


def _load_method_catalog_uncached() -> dict[str, dict[str, Any]]:
    """Load and validate the method catalog from YAML."""
    try:
        raw = yaml.safe_load(_METHOD_CATALOG_CONFIG_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MethodLibraryError(f"method catalog unreadable: {_METHOD_CATALOG_CONFIG_PATH}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise MethodLibraryError("method catalog schema_version must be 1")
    methods = raw.get("methods")
    if not isinstance(methods, list) or not methods:
        raise MethodLibraryError("method catalog must contain a non-empty methods list")
    required = {
        "id",
        "display_label",
        "description",
        "when_to_use",
        "when_not_to_use",
        "expected_cost",
        "known_failure_modes",
        "compatible_task_profiles",
    }
    enum_ids = {kind.value for kind in MethodKind}
    catalog: dict[str, dict[str, Any]] = {}
    for row in methods:
        if not isinstance(row, dict):
            raise MethodLibraryError("method catalog rows must be mappings")
        missing = required - set(row)
        if missing:
            raise MethodLibraryError(f"method catalog row missing keys: {sorted(missing)}")
        method_id = str(row["id"])
        if method_id not in enum_ids:
            raise MethodLibraryError(f"method catalog id absent from MethodKind: {method_id!r}")
        for key in ("when_to_use", "when_not_to_use", "known_failure_modes", "compatible_task_profiles"):
            if not isinstance(row[key], list) or not row[key]:
                raise MethodLibraryError(f"method catalog {method_id!r} has empty {key}")
        catalog[method_id] = _copy_catalog_row(row)
    missing_enum = enum_ids - set(catalog)
    if missing_enum:
        raise MethodLibraryError(f"method catalog missing MethodKind ids: {sorted(missing_enum)}")
    return catalog


def load_method_catalog() -> dict[str, dict[str, Any]]:
    """Return a defensive copy of the method catalog.

    Returns:
        Resolved method catalog value.
    """
    if _METHOD_CATALOG_CACHE:
        return {key: _copy_catalog_row(value) for key, value in _METHOD_CATALOG_CACHE.items()}
    with _METHOD_CATALOG_LOCK:
        if _METHOD_CATALOG_CACHE:
            return {key: _copy_catalog_row(value) for key, value in _METHOD_CATALOG_CACHE.items()}
        _METHOD_CATALOG_CACHE.update(_load_method_catalog_uncached())
        return {key: _copy_catalog_row(value) for key, value in _METHOD_CATALOG_CACHE.items()}


def _method_kind_from_metric(metric: RunMetric) -> MethodKind | None:
    if metric.name != _METHOD_KIND_METRIC_NAME:
        return None
    try:
        return MethodKind(metric.unit)
    except ValueError:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return None


def _method_kind_for_run(run: WorkbenchRun) -> MethodKind | None:
    for metric in run.metrics:
        kind = _method_kind_from_metric(metric)
        if kind is not None:
            return kind
    return None


def _score_sign(score: EvalScore) -> str:
    return "positive" if score.passed else "negative"


def _summarize_measured_deltas(
    method_evals: tuple[EvalResult, ...],
    all_evals: tuple[EvalResult, ...],
) -> tuple[MeasuredDelta, ...]:
    """Convert method eval scores into deltas against measured control baselines."""
    try:
        deltas = summarize_measured_deltas(
            method_evals,
            all_evals,
            minimum_sample_count=MIN_EVALUATIONS_FOR_ESTIMATE,
            minimum_baseline_samples=MIN_EVALUATIONS_FOR_ESTIMATE,
        )
    except MeasuredDeltaError:
        logger.warning("Method measured deltas unavailable; failing closed for promotion", exc_info=True)
        return ()
    return tuple(
        MeasuredDelta(
            metric_name=delta.metric_name,
            baseline_value=delta.baseline_value,
            method_value=delta.method_value,
            delta=delta.delta,
            sign=delta.sign,
            evidence_eval_id=delta.evidence_eval_id,
            captured_at_utc=delta.captured_at_utc,
            baseline_sample_count=delta.baseline_sample_count,
            method_sample_count=delta.method_sample_count,
            baseline_source=delta.baseline_source,
            baseline_eval_ids=delta.baseline_eval_ids,
            p_value=delta.p_value,
            effect_size=delta.effect_size,
            minimum_sample_count=delta.minimum_sample_count,
        )
        for delta in deltas
    )


def _sign_for_eval(eval_result: EvalResult) -> str:
    signs = {_score_sign(score) for score in eval_result.scores}
    if signs == {"positive"}:
        return "positive"
    if signs == {"negative"}:
        return "negative"
    return "mixed"


def _collect_evidence_refs_for_method(
    method_kind: MethodKind,
    *,
    runs: tuple[WorkbenchRun, ...],
    evals: tuple[EvalResult, ...],
    proposals: tuple[WorkbenchProposal, ...],
) -> tuple[tuple[MethodEvidenceRef, ...], tuple[MeasuredDelta, ...]]:
    """Join method-tagged runs to evals and proposal references."""
    run_ids = {run.run_id for run in runs if _method_kind_for_run(run) is method_kind}
    if not run_ids:
        return (), ()
    proposal_by_eval_id: dict[str, str] = {}
    for proposal in proposals:
        if not proposal.gate.eval_present:
            continue
        for eval_result in proposal.pre_promotion_evals:
            proposal_by_eval_id.setdefault(eval_result.eval_id, proposal.proposal_id)
    refs: list[MethodEvidenceRef] = []
    deltas: list[MeasuredDelta] = []
    for eval_result in evals:
        if eval_result.run_id not in run_ids:
            continue
        sign = _sign_for_eval(eval_result)
        refs.append(
            MethodEvidenceRef(
                eval_id=eval_result.eval_id,
                proposal_id=proposal_by_eval_id.get(eval_result.eval_id, ""),
                sign=sign,
                captured_at_utc=eval_result.captured_at_utc,
                summary=eval_result.notes or f"{eval_result.kind.value} eval {eval_result.eval_id} was {sign}",
            ),
        )
    method_evals = tuple(eval_result for eval_result in evals if eval_result.run_id in run_ids)
    deltas.extend(_summarize_measured_deltas(method_evals, evals))
    return tuple(refs), tuple(deltas)


def _derive_promotion_status(
    evidence_refs: tuple[MethodEvidenceRef, ...],
    measured_deltas: tuple[MeasuredDelta, ...],
) -> PromotionStatus:
    """Derive a fail-closed promotion status from measured evidence."""
    signs = {delta.sign for delta in measured_deltas}
    signs.discard("neutral")
    if not signs:
        return PromotionStatus.NOT_PROMOTABLE
    if "positive" in signs and "negative" in signs:
        return PromotionStatus.MEASURED_MIXED
    if "negative" in signs:
        return PromotionStatus.MEASURED_NEGATIVE
    if "positive" in signs:
        return PromotionStatus.MEASURED_POSITIVE
    return PromotionStatus.NOT_PROMOTABLE


class MethodPromotionGate:
    """Fail-closed governance gate for method promotion."""

    def require_measured_evidence(self, card: MethodCard, *, target_status: PromotionStatus) -> None:
        """Reject promotion unless the card carries measured positive evidence.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if target_status is not PromotionStatus.PROMOTED:
            return
        if not card.evidence_refs and not card.measured_deltas:
            raise MethodPromotionRejected(
                f"method {card.method_card_id!r} cannot be promoted without linked measured evidence",
            )
        positive_deltas = any(
            delta.sign == "positive"
            and delta.method_sample_count >= delta.minimum_sample_count
            and delta.baseline_sample_count >= delta.minimum_sample_count
            and delta.p_value <= 0.05
            and delta.effect_size > 0.0
            for delta in card.measured_deltas
        )
        if not positive_deltas:
            raise MethodPromotionRejected(
                f"method {card.method_card_id!r} cannot be promoted without minimum-N p-value measured evidence",
            )


class MethodLibrary:
    """Read-only builder for measured method cards."""

    def __init__(self, *, spine: WorkbenchSpine | None = None) -> None:
        self._spine = spine
        self._spine_lock = threading.Lock()
        self._read_lock = threading.Lock()

    def _get_or_init_spine(self) -> WorkbenchSpine:
        if self._spine is not None:
            return self._spine
        with self._spine_lock:
            if self._spine is not None:
                return self._spine
            try:
                self._spine = get_workbench_spine()
            except WorkbenchSpineCorrupt as exc:
                raise MethodLibraryError("workbench spine unavailable for method library") from exc
            return self._spine

    def list_cards(
        self,
        *,
        project_id: str = "default",
        kind: MethodKind | None = None,
        promotion_status: PromotionStatus | None = None,
        limit: int | None = None,
    ) -> tuple[MethodCard, ...]:
        """Return method cards derived from the catalog and live spine evidence.

        Returns:
            Collection of cards values.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        canonical = _canonicalize_project_id(project_id)
        spine = self._get_or_init_spine()
        with self._read_lock:
            try:
                runs = tuple(run for run in spine.list_runs() if run.project_id == canonical)
                run_ids = {run.run_id for run in runs}
                evals = tuple(eval_result for eval_result in spine.list_evals() if eval_result.run_id in run_ids)
                eval_ids = {eval_result.eval_id for eval_result in evals}
                proposals = tuple(
                    proposal
                    for proposal in spine.list_proposals()
                    if any(eval_result.eval_id in eval_ids for eval_result in proposal.pre_promotion_evals)
                )
            except WorkbenchSpineCorrupt as exc:
                raise MethodLibraryError("workbench spine cannot serve method evidence") from exc
        catalog = load_method_catalog()
        kinds = [kind] if kind is not None else list(MethodKind)
        cards = [
            self._build_card(
                method_kind=method_kind,
                project_id=canonical,
                catalog_row=catalog[method_kind.value],
                runs=runs,
                evals=evals,
                proposals=proposals,
            )
            for method_kind in kinds
        ]
        if promotion_status is not None:
            cards = [card for card in cards if card.promotion_status is promotion_status]
        if limit is not None:
            cards = cards[:limit]
        return tuple(cards)

    def get_card(self, *, project_id: str = "default", method_card_id: str) -> MethodCard | None:
        """Return one card by stable id or method-kind value.

        Returns:
            Resolved card value.
        """
        for card in self.list_cards(project_id=project_id):
            if card.method_card_id == method_card_id or card.kind.value == method_card_id:
                return card
        return None

    def record_prompt_method_card(
        self,
        *,
        project_id: str = "default",
        agent_type: str,
        variant_id: str,
        prompt_text: str,
        quality_score: float,
        baseline_score: float,
        provenance_ref: str,
        consent_ref: str,
        safety_ref: str,
        confidence: float,
        promoted_by: str = "prompt_evolver",
    ) -> MethodCard:
        """Persist a promoted prompt variant as measured MethodLibrary evidence.

        Returns:
            The stored method card.

        Raises:
            MethodPromotionRejected: if promotion inputs are incomplete or fail
                measured-evidence gates.
        """
        canonical = _canonicalize_project_id(project_id)
        spine = self._get_or_init_spine()
        record_prompt_method_evidence(
            spine=spine,
            project_id=canonical,
            agent_type=agent_type,
            variant_id=variant_id,
            prompt_text=prompt_text,
            quality_score=quality_score,
            baseline_score=baseline_score,
            provenance_ref=provenance_ref,
            consent_ref=consent_ref,
            safety_ref=safety_ref,
            confidence=confidence,
            promoted_by=promoted_by,
            method_kind_value=MethodKind.RED_TEAM_PROMPT_MUTATION.value,
            method_kind_metric_name=_METHOD_KIND_METRIC_NAME,
            min_evaluations=MIN_EVALUATIONS_FOR_ESTIMATE,
        )

        card = self.get_card(project_id=canonical, method_card_id=MethodKind.RED_TEAM_PROMPT_MUTATION.value)
        if card is None:
            raise MethodLibraryError("prompt method promotion did not produce a MethodLibrary card")
        MethodPromotionGate().require_measured_evidence(card, target_status=PromotionStatus.PROMOTED)
        return card

    def list_negative_methods(
        self,
        *,
        project_id: str = "default",
        task_profile: str | None = None,
    ) -> tuple[MethodCard, ...]:
        """Return measured-negative methods, optionally filtered by task profile.

        Returns:
            Collection of negative methods values.
        """
        cards = self.list_cards(project_id=project_id, promotion_status=PromotionStatus.MEASURED_NEGATIVE)
        if task_profile:
            cards = tuple(card for card in cards if task_profile in card.compatible_task_profiles)
        return cards

    @staticmethod
    def _build_card(
        *,
        method_kind: MethodKind,
        project_id: str,
        catalog_row: dict[str, Any],
        runs: tuple[WorkbenchRun, ...],
        evals: tuple[EvalResult, ...],
        proposals: tuple[WorkbenchProposal, ...],
    ) -> MethodCard:
        evidence_refs, measured_deltas = _collect_evidence_refs_for_method(
            method_kind,
            runs=runs,
            evals=evals,
            proposals=proposals,
        )
        updated_at = max((ref.captured_at_utc for ref in evidence_refs), default="")
        return MethodCard(
            method_card_id=f"{project_id}:{method_kind.value}",
            kind=method_kind,
            name=str(catalog_row["display_label"]),
            description=str(catalog_row["description"]),
            when_to_use=tuple(str(row) for row in catalog_row["when_to_use"]),
            when_not_to_use=tuple(str(row) for row in catalog_row["when_not_to_use"]),
            expected_cost=str(catalog_row["expected_cost"]),
            known_failure_modes=tuple(str(row) for row in catalog_row["known_failure_modes"]),
            compatible_task_profiles=tuple(str(row) for row in catalog_row["compatible_task_profiles"]),
            measured_deltas=measured_deltas,
            evidence_refs=evidence_refs,
            promotion_status=_derive_promotion_status(evidence_refs, measured_deltas),
            project_id=project_id,
            updated_at_utc=updated_at,
        )


__all__ = [
    "MeasuredDelta",
    "MethodCard",
    "MethodEvidenceRef",
    "MethodKind",
    "MethodLibrary",
    "MethodLibraryError",
    "MethodLibraryProjectIdRejected",
    "MethodPromotionGate",
    "MethodPromotionRejected",
    "PromotionStatus",
    "load_method_catalog",
]
