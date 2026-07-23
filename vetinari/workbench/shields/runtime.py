"""Fail-closed Workbench shield pack loader and evaluator."""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from vetinari.workbench.shields.contracts import (
    SCHEMA_VERSION,
    ShieldDecision,
    ShieldDecisionValue,
    ShieldEvaluationRequest,
    ShieldFixture,
    ShieldFixtureKind,
    ShieldRiskDomain,
    ShieldRolloutState,
    WorkbenchShieldPack,
    WorkbenchShieldPackError,
)

logger = logging.getLogger(__name__)


_DEFAULT_SHIELD_DIR = Path("config") / "workbench" / "shields"
_DEFAULT_SHIELD_FILE = "core.yaml"
_INSTANCE: WorkbenchShieldRuntime | None = None
_INSTANCE_LOCK = threading.Lock()


def _resolve_catalog_path(path: Path | str | None = None) -> Path:
    if path is None:
        return (Path.cwd() / _DEFAULT_SHIELD_DIR / _DEFAULT_SHIELD_FILE).resolve()
    candidate = Path(path)
    if len(candidate.parts) == 1:
        candidate = _DEFAULT_SHIELD_DIR / candidate
    if candidate.is_absolute():
        raise WorkbenchShieldPackError("unreadable_shield_catalog", "absolute shield catalog paths are rejected")
    if any(part == ".." for part in candidate.parts):
        raise WorkbenchShieldPackError("unreadable_shield_catalog", "shield catalog traversal is rejected")
    root = Path.cwd().resolve()
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root):
        raise WorkbenchShieldPackError("unreadable_shield_catalog", "shield catalog escapes repository root")
    return resolved


def load_shield_pack_catalog(path: Path | str | None = None) -> tuple[WorkbenchShieldPack, ...]:
    """Load shield pack YAML and fail closed on missing or corrupt trust data.

    Returns:
        Resolved shield pack catalog value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    catalog_path = _resolve_catalog_path(path)
    if not catalog_path.exists():
        raise WorkbenchShieldPackError("unreadable_shield_catalog", f"shield catalog not found: {catalog_path}")
    try:
        doc = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    except PermissionError as exc:
        raise WorkbenchShieldPackError(
            "unreadable_shield_catalog", f"shield catalog unreadable: {catalog_path}"
        ) from exc
    except (OSError, yaml.YAMLError) as exc:
        raise WorkbenchShieldPackError("unreadable_shield_catalog", f"shield catalog invalid: {exc}") from exc
    if not isinstance(doc, Mapping):
        raise WorkbenchShieldPackError("invalid_shield_catalog", "shield catalog root must be a mapping")
    if str(doc.get("schema_version", "")) != SCHEMA_VERSION:
        raise WorkbenchShieldPackError("schema_version_mismatch", f"expected schema_version {SCHEMA_VERSION}")
    raw_packs = doc.get("packs")
    if not isinstance(raw_packs, list) or not raw_packs:
        raise WorkbenchShieldPackError("invalid_shield_catalog", "shield catalog must contain non-empty packs")
    packs: list[WorkbenchShieldPack] = []
    seen: set[str] = set()
    for raw in raw_packs:
        if not isinstance(raw, Mapping):
            raise WorkbenchShieldPackError("invalid_shield_pack", "shield pack rows must be mappings")
        pack = WorkbenchShieldPack.from_mapping(raw)
        if pack.pack_id in seen:
            raise WorkbenchShieldPackError("duplicate_pack_id", f"duplicate shield pack id {pack.pack_id}")
        seen.add(pack.pack_id)
        packs.append(pack)
    return tuple(packs)


class WorkbenchShieldRuntime:
    """Read-only evaluator for selected shield packs."""

    def __init__(
        self, *, catalog_path: Path | str | None = None, packs: tuple[WorkbenchShieldPack, ...] | None = None
    ) -> None:
        loaded = packs if packs is not None else load_shield_pack_catalog(catalog_path)
        self._packs = {pack.pack_id: pack for pack in loaded}
        if len(self._packs) != len(loaded):
            raise WorkbenchShieldPackError("duplicate_pack_id", "duplicate shield pack id")

    def list_packs(self) -> tuple[WorkbenchShieldPack, ...]:
        return tuple(self._packs.values())

    def get_pack(self, pack_id: str) -> WorkbenchShieldPack:
        """Execute the get pack operation.

        Returns:
            Resolved pack value.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        try:
            return self._packs[pack_id]
        except KeyError as exc:
            raise WorkbenchShieldPackError("unknown_shield_pack", f"unknown shield pack {pack_id!r}") from exc

    def evaluate(self, request: ShieldEvaluationRequest) -> ShieldDecision:
        """Execute the evaluate operation.

        Returns:
            ShieldDecision value produced by evaluate().
        """
        pack = self._packs.get(request.pack_id)
        if pack is None:
            return _decision(
                ShieldDecisionValue.BLOCK,
                pack_id=request.pack_id,
                rule_id="shield-pack-selection",
                reason_code="unknown_shield_pack",
                policy_version="unknown",
                evidence_refs=request.evidence_refs or ("shield-runtime:unknown-pack",),
                details={"branch": "unknown_pack"},
            )
        if pack.rollout_state not in {ShieldRolloutState.ACTIVE, ShieldRolloutState.CANARY}:
            return _pack_decision(
                pack, ShieldDecisionValue.BLOCK, "shield_pack_not_selectable", request, "rollout_state"
            )
        if request.scope not in pack.selectable_scopes:
            return _pack_decision(pack, ShieldDecisionValue.BLOCK, "shield_scope_not_selectable", request, "scope")
        requested_domain = _coerce_domain(request.risk_domain or pack.risk_domain)
        if requested_domain is None:
            return _pack_decision(pack, ShieldDecisionValue.ESCALATE, "unknown_risk_domain", request, "unknown_domain")
        if requested_domain is not pack.risk_domain:
            return _pack_decision(pack, ShieldDecisionValue.BLOCK, "risk_domain_mismatch", request, "domain_mismatch")
        if (request.tool_surface_ids or request.capability_pack_ids) and not request.metadata.get(
            "authority_gate_verified"
        ):
            return _pack_decision(
                pack,
                ShieldDecisionValue.BLOCK,
                "authority_gate_required",
                request,
                "authority_gate_missing",
            )

        rule = pack.rules[0]
        fixture = self._select_fixture(pack, request)
        if fixture.kind is ShieldFixtureKind.KNOWN_BAD:
            value = fixture.expected_decision
            return _decision(
                value,
                pack_id=pack.pack_id,
                rule_id=rule.rule_id,
                reason_code=rule.reason_code,
                policy_version=pack.policy_version,
                evidence_refs=fixture.evidence_refs,
                details={
                    "branch": "known_bad_fixture",
                    "fixture_id": fixture.fixture_id,
                    "risk_domain": pack.risk_domain.value,
                    "protected_surface_refs": list(rule.protected_surface_refs),
                },
            )
        value = ShieldDecisionValue.ALLOW if pack.default_mode is pack.default_mode.STRICT else ShieldDecisionValue.WARN
        return _decision(
            value,
            pack_id=pack.pack_id,
            rule_id=rule.rule_id,
            reason_code="allowed",
            policy_version=pack.policy_version,
            evidence_refs=fixture.evidence_refs,
            details={
                "branch": "known_good_fixture",
                "fixture_id": fixture.fixture_id,
                "risk_domain": pack.risk_domain.value,
                "protected_surface_refs": list(rule.protected_surface_refs),
            },
        )

    @staticmethod
    def _select_fixture(pack: WorkbenchShieldPack, request: ShieldEvaluationRequest) -> ShieldFixture:
        if request.fixture_id:
            for fixture in pack.fixtures:
                if fixture.fixture_id == request.fixture_id:
                    return fixture
            raise WorkbenchShieldPackError("unknown_fixture", f"unknown fixture {request.fixture_id!r}")
        target = request.action_summary.lower()
        for rule in pack.rules:
            if any(pattern.lower() in target for pattern in rule.block_patterns):
                return _first_fixture(pack, rule.bad_fixture_refs)
        return _first_fixture(pack, pack.rules[0].good_fixture_refs)


def _coerce_domain(value: ShieldRiskDomain | str | None) -> ShieldRiskDomain | None:
    if isinstance(value, ShieldRiskDomain):
        return value
    raw_value = value.value if isinstance(value, Enum) else value
    try:
        return ShieldRiskDomain(raw_value)
    except ValueError:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return None


def _first_fixture(pack: WorkbenchShieldPack, fixture_refs: tuple[str, ...]) -> ShieldFixture:
    fixture_map = {fixture.fixture_id: fixture for fixture in pack.fixtures}
    for fixture_id in fixture_refs:
        if fixture_id in fixture_map:
            return fixture_map[fixture_id]
    raise WorkbenchShieldPackError("unknown_fixture", f"{pack.pack_id} references unavailable fixture")


def _pack_decision(
    pack: WorkbenchShieldPack,
    value: ShieldDecisionValue,
    reason_code: str,
    request: ShieldEvaluationRequest,
    branch: str,
) -> ShieldDecision:
    return _decision(
        value,
        pack_id=pack.pack_id,
        rule_id="shield-pack-selection",
        reason_code=reason_code,
        policy_version=pack.policy_version,
        evidence_refs=request.evidence_refs or (f"shield-pack:{pack.pack_id}",),
        details={"branch": branch, "risk_domain": pack.risk_domain.value},
    )


def _decision(
    value: ShieldDecisionValue,
    *,
    pack_id: str,
    rule_id: str,
    reason_code: str,
    policy_version: str,
    evidence_refs: tuple[str, ...],
    details: Mapping[str, Any],
) -> ShieldDecision:
    return ShieldDecision(
        value=value,
        pack_id=pack_id,
        rule_id=rule_id,
        reason_code=reason_code,
        policy_version=policy_version,
        evidence_refs=evidence_refs,
        details=details,
    )


def get_workbench_shields() -> WorkbenchShieldRuntime:
    """Return the process singleton using double-checked locking.

    Returns:
        Resolved workbench shields value.
    """
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                runtime = WorkbenchShieldRuntime()
                _INSTANCE = runtime
    return _INSTANCE


def reset_workbench_shields_for_test() -> None:
    """Reset the shield runtime singleton for deterministic tests."""
    global _INSTANCE
    with _INSTANCE_LOCK:
        _INSTANCE = None
