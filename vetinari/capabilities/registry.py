"""Capability registry and append-only runtime state."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tomllib
import yaml

from vetinari.capabilities.types import (
    CapabilityHealthState,
    CapabilityInstallState,
    CapabilityKind,
    CapabilityMetadata,
    CapabilityNotFound,
    CapabilityProbeResult,
    CapabilityRegistryError,
    CapabilityRiskLevel,
    CapabilityState,
    DetectionRule,
    DetectionRuleKind,
)
from vetinari.constants import OUTPUTS_DIR
from vetinari.learning.atomic_writers import append_jsonl_atomic

_INSTANCE: CapabilityRegistry | None = None
_INSTANCE_LOCK = threading.Lock()
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_PATH = _REPO_ROOT / "config" / "capabilities.yaml"
_DEFAULT_STATE_DIR = OUTPUTS_DIR / "capabilities"
_STATE_FILENAME = "state.jsonl"
_SCHEMA_VERSION = 1


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_kind(kind: CapabilityKind) -> CapabilityKind:
    if isinstance(kind, CapabilityKind):
        return kind
    try:
        return CapabilityKind(getattr(kind, "value", kind))
    except ValueError as exc:
        raise CapabilityNotFound(f"capability lookup expected CapabilityKind, got {type(kind).__name__}") from exc


class CapabilityRegistry:
    """In-memory catalog plus append-only runtime state for capabilities."""

    def __init__(
        self, *, config_path: Path | str = _DEFAULT_CONFIG_PATH, state_dir: Path | str = _DEFAULT_STATE_DIR
    ) -> None:
        self._config_path = Path(config_path)
        self._state_dir = Path(state_dir)
        self._state_path = self._state_dir / _STATE_FILENAME
        self._state_lock = threading.Lock()
        self._catalog = self._load_catalog()
        self._state = {kind: self._initial_state(kind) for kind in self._catalog}
        self._ensure_state_dir()
        self._replay_state()

    def lookup(self, kind: CapabilityKind) -> CapabilityMetadata:
        """Return metadata for ``kind`` or fail closed.

        Returns:
            CapabilityMetadata value produced by lookup().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        kind = _coerce_kind(kind)
        metadata = self._catalog.get(kind)
        if metadata is None:
            raise CapabilityNotFound(f"no registered capability for kind {kind.value!r}")
        return metadata

    def list_capabilities(self) -> tuple[CapabilityMetadata, ...]:
        """Return every registered capability ordered by kind value."""
        return tuple(self._catalog[kind] for kind in sorted(self._catalog, key=lambda item: item.value))

    def get_state(self, kind: CapabilityKind) -> CapabilityState:
        """Return runtime state for ``kind`` or fail closed.

        Returns:
            Resolved state value.
        """
        kind = self.lookup(kind).kind
        return self._state[kind]

    def is_available(self, kind: CapabilityKind) -> bool:
        """Return true only when the capability is installed and healthy.

        Returns:
            Boolean indicating whether is available.
        """
        state = self.get_state(kind)
        return (
            state.install_state is CapabilityInstallState.INSTALLED
            and state.health_state is CapabilityHealthState.HEALTHY
        )

    def record_install_attempt(self, kind: CapabilityKind) -> CapabilityState:
        """Record the start of an install attempt.

        Returns:
            Outcome produced by record_install_attempt().
        """
        kind = self.lookup(kind).kind
        now = _utc_now_iso()
        with self._state_lock:
            old = self.get_state(kind)
            new = replace(
                old,
                install_state=CapabilityInstallState.INSTALLING,
                last_install_attempt_utc=now,
                last_decline_utc=None,
                install_failure_reason=None,
            )
            self._append_transition(kind, "install_attempt", old, new, now)
            self._state[kind] = new
            return new

    def record_install_success(self, kind: CapabilityKind) -> CapabilityState:
        """Record successful install completion.

        Returns:
            Outcome produced by record_install_success().
        """
        kind = self.lookup(kind).kind
        now = _utc_now_iso()
        with self._state_lock:
            old = self.get_state(kind)
            new = replace(
                old, install_state=CapabilityInstallState.INSTALLED, install_failure_reason=None, last_decline_utc=None
            )
            self._append_transition(kind, "install_success", old, new, now)
            self._state[kind] = new
            return new

    def record_install_failure(self, kind: CapabilityKind, *, reason: str) -> CapabilityState:
        """Record failed install completion.

        Returns:
            Outcome produced by record_install_failure().
        """
        kind = self.lookup(kind).kind
        now = _utc_now_iso()
        with self._state_lock:
            old = self.get_state(kind)
            new = replace(
                old,
                install_state=CapabilityInstallState.INSTALL_FAILED,
                install_failure_reason=reason,
                last_decline_utc=None,
            )
            self._append_transition(kind, "install_failure", old, new, now, install_failure_reason=reason)
            self._state[kind] = new
            return new

    def record_decline(self, kind: CapabilityKind, *, decline_reason: str) -> CapabilityState:
        """Record a user decline without making the capability permanently unavailable.

        Returns:
            Outcome produced by record_decline().
        """
        kind = self.lookup(kind).kind
        now = _utc_now_iso()
        with self._state_lock:
            old = self.get_state(kind)
            new = replace(
                old,
                install_state=CapabilityInstallState.DECLINED_FOR_NOW,
                last_decline_utc=now,
                install_failure_reason=None,
            )
            self._append_transition(kind, "decline", old, new, now, decline_reason=decline_reason)
            self._state[kind] = new
            return new

    def record_health_probe(self, probe_result: CapabilityProbeResult) -> CapabilityState:
        """Persist the latest health probe result.

        Returns:
            Outcome produced by record_health_probe().
        """
        kind = self.lookup(probe_result.kind).kind
        now = probe_result.probed_at_utc or _utc_now_iso()
        with self._state_lock:
            old = self.get_state(kind)
            new = replace(old, health_state=probe_result.health_state, last_checked_utc=now)
            self._append_transition(kind, "health_probe", old, new, now, probe_error=probe_result.error)
            self._state[kind] = new
            return new

    def _load_catalog(self) -> dict[CapabilityKind, CapabilityMetadata]:
        try:
            raw = yaml.safe_load(self._config_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise CapabilityRegistryError(f"could not read capability config {self._config_path}: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
            raise CapabilityRegistryError("capability config schema_version mismatch")
        rows = raw.get("capabilities")
        if not isinstance(rows, list):
            raise CapabilityRegistryError("capability config must contain a capabilities list")
        legal_envs = self._load_runtime_tiers()
        extras = self._load_pyproject_extras()
        catalog: dict[CapabilityKind, CapabilityMetadata] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise CapabilityRegistryError("capability row must be a mapping")
            metadata = self._metadata_from_row(row, legal_envs=legal_envs, extras=extras)
            if metadata.kind in catalog:
                raise CapabilityRegistryError(f"duplicate capability kind {metadata.kind.value!r}")
            catalog[metadata.kind] = metadata
        if set(catalog) != set(CapabilityKind):
            missing = sorted(kind.value for kind in set(CapabilityKind) - set(catalog))
            extra = sorted(kind.value for kind in set(catalog) - set(CapabilityKind))
            raise CapabilityRegistryError(f"capability YAML/enum mismatch missing={missing} extra={extra}")
        return catalog

    @staticmethod
    def _metadata_from_row(row: dict[str, Any], *, legal_envs: set[str], extras: set[str]) -> CapabilityMetadata:
        try:
            kind = CapabilityKind(row["kind"])
            target_environment = str(row["target_environment"])
            pip_extra = str(row["pip_extra"])
            risk_level = CapabilityRiskLevel(row["risk_level"])
            rule_raw = row["detection_rule"]
            rule = DetectionRule(
                kind=DetectionRuleKind(rule_raw["kind"]),
                target=str(rule_raw["target"]),
                timeout_s=float(rule_raw.get("timeout_s", 2.0)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CapabilityRegistryError(f"invalid capability row {row!r}: {exc}") from exc
        if target_environment not in legal_envs:
            raise CapabilityRegistryError(f"{kind.value}: unknown target_environment {target_environment!r}")
        if pip_extra not in extras:
            raise CapabilityRegistryError(f"{kind.value}: pip_extra {pip_extra!r} not declared in pyproject.toml")
        return CapabilityMetadata(
            kind=kind,
            display_name=str(row["display_name"]),
            description=str(row["description"]),
            target_environment=target_environment,
            pip_extra=pip_extra,
            extra_packages=tuple(str(item) for item in row.get("extra_packages", ())),
            disk_impact_mb=int(row["disk_impact_mb"]),
            network_impact_mb=int(row["network_impact_mb"]),
            requires_native_binary=bool(row["requires_native_binary"]),
            requires_wsl=bool(row["requires_wsl"]),
            requires_credentials=tuple(str(item) for item in row.get("requires_credentials", ())),
            risk_level=risk_level,
            degraded_fallback=str(row["degraded_fallback"]),
            uninstall_note=str(row["uninstall_note"]),
            detection_rule=rule,
        )

    @staticmethod
    def _load_runtime_tiers() -> set[str]:
        try:
            raw = yaml.safe_load((_REPO_ROOT / "config" / "runtime_environments.yaml").read_text(encoding="utf-8"))
        except OSError as exc:
            raise CapabilityRegistryError(f"could not read runtime environments: {exc}") from exc
        tiers = raw.get("tiers") if isinstance(raw, dict) else None
        if not isinstance(tiers, dict):
            raise CapabilityRegistryError("runtime environment config must contain tiers")
        return set(tiers)

    @staticmethod
    def _load_pyproject_extras() -> set[str]:
        try:
            raw = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise CapabilityRegistryError(f"could not read pyproject extras: {exc}") from exc
        extras = raw.get("project", {}).get("optional-dependencies", {})
        if not isinstance(extras, dict):
            raise CapabilityRegistryError("pyproject.toml has no optional dependency table")
        return set(extras)

    def _ensure_state_dir(self) -> None:
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CapabilityRegistryError(
                f"could not create capability state directory {self._state_dir}: {exc}"
            ) from exc
        if not self._state_dir.is_dir():
            raise CapabilityRegistryError(f"capability state path is not a directory: {self._state_dir}")

    def _replay_state(self) -> None:
        if not self._state_path.exists():
            return
        try:
            lines = self._state_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise CapabilityRegistryError(f"could not read capability state file: {exc}") from exc
        for index, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                kind = CapabilityKind(event["kind"])
                self.lookup(kind)
                self._state[kind] = CapabilityState(
                    kind=kind,
                    install_state=CapabilityInstallState(event["to_install_state"]),
                    health_state=CapabilityHealthState(event["to_health_state"]),
                    last_checked_utc=event.get("last_checked_utc"),
                    last_install_attempt_utc=event.get("last_install_attempt_utc"),
                    last_decline_utc=event.get("last_decline_utc"),
                    install_failure_reason=event.get("install_failure_reason"),
                )
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise CapabilityRegistryError(f"corrupt capability state JSONL at line {index}: {exc}") from exc

    def _append_transition(
        self,
        kind: CapabilityKind,
        transition: str,
        old: CapabilityState,
        new: CapabilityState,
        transitioned_at_utc: str,
        *,
        decline_reason: str | None = None,
        install_failure_reason: str | None = None,
        probe_error: str | None = None,
    ) -> None:
        event = {
            "kind": kind.value,
            "transition": transition,
            "from_install_state": old.install_state.value,
            "to_install_state": new.install_state.value,
            "from_health_state": old.health_state.value,
            "to_health_state": new.health_state.value,
            "transitioned_at_utc": transitioned_at_utc,
            "last_checked_utc": new.last_checked_utc,
            "last_install_attempt_utc": new.last_install_attempt_utc,
            "last_decline_utc": new.last_decline_utc,
            "decline_reason": decline_reason,
            "install_failure_reason": install_failure_reason or new.install_failure_reason,
            "probe_error": probe_error,
        }
        try:
            append_jsonl_atomic(self._state_path, event)
        except OSError as exc:
            raise CapabilityRegistryError(f"could not append capability state event: {exc}") from exc

    @staticmethod
    def _initial_state(kind: CapabilityKind) -> CapabilityState:
        return CapabilityState(
            kind, CapabilityInstallState.NOT_INSTALLED, CapabilityHealthState.UNKNOWN, None, None, None, None
        )


def get_capability_registry() -> CapabilityRegistry:
    """Return the process-wide capability registry singleton.

    Returns:
        Resolved capability registry value.
    """
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = CapabilityRegistry()
    return _INSTANCE


def reset_capability_registry_for_test() -> None:
    """Clear the process-wide registry singleton for test isolation."""
    global _INSTANCE
    with _INSTANCE_LOCK:
        _INSTANCE = None


__all__ = ["CapabilityRegistry", "get_capability_registry", "reset_capability_registry_for_test"]
