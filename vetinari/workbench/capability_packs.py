"""Trusted, inspectable Workbench capability-pack catalog."""

from __future__ import annotations

import importlib
import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from vetinari.capabilities.types import CapabilityKind
from vetinari.constants import PROJECT_ROOT
from vetinari.workbench.gateway_policy import GatewayPolicyError, WorkbenchGatewayPolicy, get_workbench_gateway_policy

logger = logging.getLogger(__name__)


_CATALOG_SCHEMA_VERSION = 1
_CATALOG_DIR = PROJECT_ROOT / "config" / "workbench" / "capability_packs"
_REQUIRED_LIST_FIELDS = ("schemas", "policy_bindings", "smoke_evals", "known_limitations")
_REQUIRED_STRING_FIELDS = (
    "pack_id",
    "version",
    "capability_kind",
    "source",
    "credential_posture",
    "locality",
    "cost_policy",
    "freshness_policy",
    "tested_status",
    "current_status",
    "uninstall_command",
    "disable_command",
)
_ALLOWED_COST_POLICIES = frozenset({"declared", "bounded", "free"})
_ALLOWED_FRESHNESS_POLICIES = frozenset({"current", "refreshable"})
_ALLOWED_CREDENTIAL_POSTURES = frozenset({"none", "optional", "required_disclosed"})
_ALLOWED_LOCALITIES = frozenset({"local", "remote", "hybrid"})

_CAPABILITY_PACK_CATALOG_LOCK = threading.Lock()
_CAPABILITY_PACK_CATALOG_CACHE: tuple[CapabilityPack, ...] | None = None


class CapabilityPackError(RuntimeError):
    """Fail-closed error raised when capability-pack trust cannot be proven."""


class CapabilityTrustStatus(str, Enum):
    """Trust posture for a capability pack."""

    TRUSTED = "trusted"
    DENIED = "denied"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class CapabilityPack:
    """Immutable catalog row for one Workbench capability unit."""

    pack_id: str
    version: str
    capability_kind: CapabilityKind
    schemas: tuple[str, ...]
    permissions: tuple[str, ...]
    policy_bindings: tuple[str, ...]
    smoke_evals: tuple[str, ...]
    examples: tuple[str, ...]
    uninstall_command: str
    disable_command: str
    known_limitations: tuple[str, ...]
    credential_posture: str
    locality: str
    source: str
    cost_policy: str
    freshness_policy: str
    tested_status: str
    current_status: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        row = asdict(self)
        row["capability_kind"] = self.capability_kind.value
        row["trust_status"] = CapabilityTrustStatus.TRUSTED.value
        return row

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CapabilityPack(pack_id={self.pack_id!r}, version={self.version!r}, capability_kind={self.capability_kind!r})"


@dataclass(frozen=True, slots=True)
class CapabilityEnablementDecision:
    """Service verdict for enable, disable, smoke-test, or uninstall actions."""

    pack_id: str
    status: CapabilityTrustStatus
    allowed: bool
    reasons: tuple[str, ...]
    missing: tuple[str, ...] = ()
    actions: Mapping[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return {
            "pack_id": self.pack_id,
            "status": self.status.value,
            "allowed": self.allowed,
            "reasons": list(self.reasons),
            "missing": list(self.missing),
            "actions": dict(self.actions),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"CapabilityEnablementDecision(pack_id={self.pack_id!r}, status={self.status!r}, allowed={self.allowed!r})"
        )


InstallerProbe = Callable[[CapabilityPack], Mapping[str, Any]]


def _as_non_empty_string(value: Any, field_name: str, *, pack_id: str | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        subject = f" for {pack_id}" if pack_id else ""
        raise CapabilityPackError(f"missing {field_name}{subject}")
    return value.strip()


def _as_string_tuple(value: Any, field_name: str, *, pack_id: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise CapabilityPackError(f"{field_name} must be a non-empty list for {pack_id}")
    rows = tuple(str(item).strip() for item in value if str(item).strip())
    if not rows:
        raise CapabilityPackError(f"missing {field_name} for {pack_id}")
    return rows


def _pack_from_mapping(raw: Mapping[str, Any]) -> CapabilityPack:
    pack_id = _as_non_empty_string(raw.get("pack_id"), "pack_id")
    for field_name in _REQUIRED_STRING_FIELDS:
        _as_non_empty_string(raw.get(field_name), field_name, pack_id=pack_id)
    list_values = {
        field_name: _as_string_tuple(raw.get(field_name), field_name, pack_id=pack_id)
        for field_name in _REQUIRED_LIST_FIELDS
    }
    try:
        capability_kind = CapabilityKind(
            _as_non_empty_string(raw.get("capability_kind"), "capability_kind", pack_id=pack_id)
        )
    except ValueError as exc:
        raise CapabilityPackError(f"unknown capability_kind for {pack_id}: {raw.get('capability_kind')!r}") from exc
    return CapabilityPack(
        pack_id=pack_id,
        version=_as_non_empty_string(raw.get("version"), "version", pack_id=pack_id),
        capability_kind=capability_kind,
        schemas=list_values["schemas"],
        permissions=tuple(str(item).strip() for item in raw.get("permissions", []) if str(item).strip()),
        policy_bindings=list_values["policy_bindings"],
        smoke_evals=list_values["smoke_evals"],
        examples=tuple(str(item).strip() for item in raw.get("examples", []) if str(item).strip()),
        uninstall_command=_as_non_empty_string(raw.get("uninstall_command"), "uninstall_command", pack_id=pack_id),
        disable_command=_as_non_empty_string(raw.get("disable_command"), "disable_command", pack_id=pack_id),
        known_limitations=list_values["known_limitations"],
        credential_posture=_as_non_empty_string(raw.get("credential_posture"), "credential_posture", pack_id=pack_id),
        locality=_as_non_empty_string(raw.get("locality"), "locality", pack_id=pack_id),
        source=_as_non_empty_string(raw.get("source"), "source", pack_id=pack_id),
        cost_policy=_as_non_empty_string(raw.get("cost_policy"), "cost_policy", pack_id=pack_id),
        freshness_policy=_as_non_empty_string(raw.get("freshness_policy"), "freshness_policy", pack_id=pack_id),
        tested_status=_as_non_empty_string(raw.get("tested_status"), "tested_status", pack_id=pack_id),
        current_status=_as_non_empty_string(raw.get("current_status"), "current_status", pack_id=pack_id),
        metadata=dict(raw.get("metadata", {}) if isinstance(raw.get("metadata", {}), Mapping) else {}),
    )


def _load_catalog_uncached(catalog_dir: Path) -> tuple[CapabilityPack, ...]:
    paths = sorted(catalog_dir.glob("*.yaml"))
    if not paths:
        raise CapabilityPackError(f"no capability-pack YAML files found in {catalog_dir}")
    rows: list[CapabilityPack] = []
    seen: set[str] = set()
    for path in paths:
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise CapabilityPackError(f"cannot read capability-pack catalog {path}: {exc}") from exc
        if not isinstance(doc, Mapping):
            raise CapabilityPackError(f"capability-pack catalog {path} must be a mapping")
        if doc.get("schema_version") != _CATALOG_SCHEMA_VERSION:
            raise CapabilityPackError(
                f"capability-pack catalog {path} schema mismatch: expected {_CATALOG_SCHEMA_VERSION}, "
                f"got {doc.get('schema_version')!r}"
            )
        raw_packs = doc.get("packs")
        if not isinstance(raw_packs, Sequence) or isinstance(raw_packs, (str, bytes)) or not raw_packs:
            raise CapabilityPackError(f"capability-pack catalog {path} must contain non-empty packs")
        for raw in raw_packs:
            if not isinstance(raw, Mapping):
                raise CapabilityPackError(f"pack row in {path} must be a mapping")
            pack = _pack_from_mapping(raw)
            if pack.pack_id in seen:
                raise CapabilityPackError(f"duplicate capability pack id {pack.pack_id}")
            seen.add(pack.pack_id)
            rows.append(pack)
    return tuple(rows)


def load_capability_pack_catalog(catalog_dir: Path | str | None = None) -> tuple[CapabilityPack, ...]:
    """Load capability-pack catalog rows, failing closed on unreadable trust data.

    Returns:
        Resolved capability pack catalog value.
    """
    global _CAPABILITY_PACK_CATALOG_CACHE
    if catalog_dir is not None:
        return _load_catalog_uncached(Path(catalog_dir))
    with _CAPABILITY_PACK_CATALOG_LOCK:
        if _CAPABILITY_PACK_CATALOG_CACHE is None:
            loaded = _load_catalog_uncached(_CATALOG_DIR)
            _CAPABILITY_PACK_CATALOG_CACHE = loaded
        return _CAPABILITY_PACK_CATALOG_CACHE


def reset_capability_pack_catalog_for_test() -> None:
    """Clear the module-level catalog cache for deterministic tests."""
    global _CAPABILITY_PACK_CATALOG_CACHE
    with _CAPABILITY_PACK_CATALOG_LOCK:
        _CAPABILITY_PACK_CATALOG_CACHE = None


def _default_installer_probe(pack: CapabilityPack) -> Mapping[str, Any]:
    """Read installer availability without mutating the environment."""
    try:
        capability_installer = importlib.import_module("vetinari.setup.capability_installer")
    except ImportError as exc:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return {
            "available": False,
            "reason": f"capability_installer unavailable: {exc}",
            "module": "vetinari.setup.capability_installer",
        }
    status_fn = getattr(capability_installer, "get_capability_install_status", None)
    if callable(status_fn):
        status = status_fn(pack.pack_id)
        if isinstance(status, Mapping):
            return status
    return {"available": True, "reason": "capability_installer importable", "module": capability_installer.__name__}


def _smoke_eval_missing_targets(pack: CapabilityPack) -> tuple[str, ...]:
    missing: list[str] = []
    for target in pack.smoke_evals:
        raw_path, _, test_name = target.partition("::")
        path = PROJECT_ROOT / raw_path
        if not path.exists():
            missing.append(target)
            continue
        if test_name:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning("Unable to read smoke-eval target %s: %s", path, exc)
                missing.append(target)
                continue
            if f"def {test_name}(" not in text and f"class {test_name}" not in text:
                missing.append(target)
    return tuple(missing)


class CapabilityPackService:
    """Evaluate trusted capability-pack enablement without direct installs."""

    def __init__(
        self,
        *,
        catalog_dir: Path | str | None = None,
        gateway_policy: WorkbenchGatewayPolicy | None = None,
        installer_probe: InstallerProbe | None = None,
        capability_registry: Any | None = None,
    ) -> None:
        self._catalog_dir = Path(catalog_dir) if catalog_dir is not None else None
        self._gateway_policy = gateway_policy
        self._installer_probe = installer_probe or _default_installer_probe
        self._capability_registry = capability_registry

    def list_packs(self) -> list[dict[str, Any]]:
        """Return all catalog packs with their current trust decision.

        Returns:
            Collection of packs values.
        """
        rows = []
        for pack in load_capability_pack_catalog(self._catalog_dir):
            row = pack.to_dict()
            row["enablement"] = self.evaluate_enablement(pack.pack_id).to_dict()
            rows.append(row)
        return rows

    def get_pack(self, pack_id: str) -> CapabilityPack:
        """Return one pack by id or fail closed.

        Returns:
            Resolved pack value.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        for pack in load_capability_pack_catalog(self._catalog_dir):
            if pack.pack_id == pack_id:
                return pack
        raise CapabilityPackError(f"capability pack {pack_id!r} not found")

    def evaluate_enablement(self, pack_id: str) -> CapabilityEnablementDecision:
        """Decide whether a pack can be enabled without mutating installer state.

        Returns:
            CapabilityEnablementDecision value produced by evaluate_enablement().
        """
        pack = self.get_pack(pack_id)
        reasons: list[str] = []
        missing: list[str] = []
        if not pack.schemas:
            missing.append("schemas")
        if not pack.policy_bindings:
            missing.append("policy_bindings")
        if not pack.smoke_evals:
            missing.append("smoke_evals")
        if not pack.uninstall_command or not pack.disable_command:
            missing.append("uninstall_or_disable_path")
        if not pack.known_limitations:
            missing.append("known_limitations")
        if pack.cost_policy not in _ALLOWED_COST_POLICIES:
            reasons.append(f"unknown cost policy {pack.cost_policy!r}")
        if pack.freshness_policy not in _ALLOWED_FRESHNESS_POLICIES:
            reasons.append(f"unknown freshness policy {pack.freshness_policy!r}")
        if pack.credential_posture not in _ALLOWED_CREDENTIAL_POSTURES:
            reasons.append(f"unknown credential posture {pack.credential_posture!r}")
        if pack.locality not in _ALLOWED_LOCALITIES:
            reasons.append(f"unknown locality {pack.locality!r}")
        if pack.tested_status != "tested":
            reasons.append(f"pack is not smoke-tested: {pack.tested_status}")
        if pack.current_status != "current":
            reasons.append(f"pack is not current: {pack.current_status}")
        missing_smoke_targets = _smoke_eval_missing_targets(pack)
        if missing_smoke_targets:
            reasons.append("smoke eval targets missing or unresolved: " + ", ".join(missing_smoke_targets))
        if not self._gateway_policy_allows(pack):
            reasons.append("gateway policy binding unavailable or denied")
        installer_status = self._installer_probe(pack)
        if not bool(installer_status.get("available")):
            reasons.append(str(installer_status.get("reason") or "installer status unavailable"))
        if missing:
            reasons.append("required trust fields missing")
        allowed = not reasons and not missing
        return CapabilityEnablementDecision(
            pack_id=pack.pack_id,
            status=CapabilityTrustStatus.TRUSTED if allowed else CapabilityTrustStatus.DENIED,
            allowed=allowed,
            reasons=tuple(reasons or ("trusted capability pack",)),
            missing=tuple(missing),
            actions={
                "enable": allowed,
                "disable": bool(pack.disable_command),
                "uninstall": bool(pack.uninstall_command),
                "smoke_test": bool(pack.smoke_evals),
            },
        )

    def enable_pack(self, pack_id: str) -> CapabilityEnablementDecision:
        """Verify a pack can be locally enabled without marking it installed.

        Returns:
            Enablement decision with reasons and allowed actions.
        """
        decision = self.evaluate_enablement(pack_id)
        if decision.allowed:
            return CapabilityEnablementDecision(
                pack_id=decision.pack_id,
                status=decision.status,
                allowed=True,
                reasons=("enablement verified; no install was performed",),
                missing=decision.missing,
                actions=decision.actions,
            )
        return decision

    def disable_pack(self, pack_id: str) -> CapabilityEnablementDecision:
        """Return a disable decision without mutating external state.

        Returns:
            CapabilityEnablementDecision value produced by disable_pack().
        """
        decision = self.evaluate_enablement(pack_id)
        pack = self.get_pack(pack_id)
        return CapabilityEnablementDecision(
            pack_id=pack_id,
            status=CapabilityTrustStatus.TRUSTED if pack.disable_command else CapabilityTrustStatus.DENIED,
            allowed=bool(pack.disable_command),
            reasons=("disable path declared",) if pack.disable_command else ("disable path missing",),
            missing=() if pack.disable_command else ("disable_command",),
            actions=decision.actions,
        )

    def uninstall_pack(self, pack_id: str) -> CapabilityEnablementDecision:
        """Return an uninstall decision without running uninstall commands.

        Returns:
            CapabilityEnablementDecision value produced by uninstall_pack().
        """
        decision = self.evaluate_enablement(pack_id)
        pack = self.get_pack(pack_id)
        return CapabilityEnablementDecision(
            pack_id=pack_id,
            status=CapabilityTrustStatus.TRUSTED if pack.uninstall_command else CapabilityTrustStatus.DENIED,
            allowed=bool(pack.uninstall_command),
            reasons=("uninstall path declared; no command was executed",)
            if pack.uninstall_command
            else ("uninstall path missing",),
            missing=() if pack.uninstall_command else ("uninstall_command",),
            actions=decision.actions,
        )

    def smoke_test_pack(self, pack_id: str) -> CapabilityEnablementDecision:
        """Return a smoke-test availability decision without running arbitrary commands.

        Returns:
            CapabilityEnablementDecision value produced by smoke_test_pack().
        """
        decision = self.evaluate_enablement(pack_id)
        pack = self.get_pack(pack_id)
        missing_targets = _smoke_eval_missing_targets(pack)
        allowed = bool(pack.smoke_evals) and not missing_targets
        return CapabilityEnablementDecision(
            pack_id=pack_id,
            status=CapabilityTrustStatus.TRUSTED if allowed else CapabilityTrustStatus.DENIED,
            allowed=allowed,
            reasons=("smoke eval target verified; no eval was executed",)
            if allowed
            else (
                ("smoke eval targets missing or unresolved: " + ", ".join(missing_targets),)
                if missing_targets
                else ("smoke eval missing",)
            ),
            missing=missing_targets or (() if pack.smoke_evals else ("smoke_evals",)),
            actions=decision.actions,
        )

    def _gateway_policy_allows(self, pack: CapabilityPack) -> bool:
        try:
            gateway_policy = self._gateway_policy or get_workbench_gateway_policy()
            profiles = gateway_policy.list_active_profiles()
        except (GatewayPolicyError, OSError, RuntimeError, ValueError):
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return False
        profile_ids = {str(profile.get("id")) for profile in profiles if isinstance(profile, Mapping)}
        return all(binding in profile_ids for binding in pack.policy_bindings)


__all__ = [
    "CapabilityEnablementDecision",
    "CapabilityPack",
    "CapabilityPackError",
    "CapabilityPackService",
    "CapabilityTrustStatus",
    "load_capability_pack_catalog",
    "reset_capability_pack_catalog_for_test",
]
