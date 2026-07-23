"""Serving lifecycle records for Workbench model registry versions.

Endpoint bindings, canaries, traffic splits, and monitor snapshots are
append-only and lock-protected. Importing this module performs no I/O.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from vetinari.api.responses import json_safe as _json_safe
from vetinari.utils.bounded_collections import BoundedList
from vetinari.workbench.model_registry import ModelStage, WorkbenchModelRegistry, WorkbenchModelRegistryError
from vetinari.workbench.spine_consumers import record_asset_written, record_lease_acquired

_DEFAULT_SERVING_DIR = Path("outputs") / "workbench" / "model-serving"
_STATE_FILENAME = "serving_lifecycle.jsonl"
_REQUIRED_MONITORS = frozenset({"health", "cost", "latency", "quality", "safety"})


class ServingLifecycleError(RuntimeError):
    """Raised when serving lifecycle state cannot be trusted."""

    def __init__(self, reason: str, *, blockers: tuple[str, ...] = (), path: Path | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.blockers = blockers
        self.path = path

    def __str__(self) -> str:
        parts = BoundedList[str](3, [f"ServingLifecycleError: {self.reason}"])
        if self.blockers:
            parts.append(f"blockers={list(self.blockers)}")
        if self.path is not None:
            parts.append(f"path={self.path}")
        return " ".join(parts)


class EndpointStatus(str, Enum):
    """Serving endpoint lifecycle status."""

    CANDIDATE = "candidate"
    CANARY = "canary"
    ACTIVE = "active"
    DRAINING = "draining"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class EndpointBinding:
    """Runtime endpoint bound to a registry version."""

    endpoint_id: str
    version_id: str
    alias: str
    runtime_kind: str
    base_url: str
    status: EndpointStatus
    traffic_weight: int
    policy_ref: str
    evidence_ids: tuple[str, ...]
    project_id: str = "default"

    def __post_init__(self) -> None:
        _require_non_empty(self.project_id, "project_id")
        _require_non_empty(self.endpoint_id, "endpoint_id")
        _require_non_empty(self.version_id, "version_id")
        _require_non_empty(self.alias, "alias")
        _require_non_empty(self.runtime_kind, "runtime_kind")
        _require_non_empty(self.base_url, "base_url")
        if self.traffic_weight < 0 or self.traffic_weight > 100:
            raise ValueError("traffic_weight must be between 0 and 100")
        _require_non_empty(self.policy_ref, "policy_ref")
        _require_string_tuple(self.evidence_ids, "evidence_ids")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"EndpointBinding(endpoint_id={self.endpoint_id!r}, version_id={self.version_id!r}, alias={self.alias!r})"
        )


@dataclass(frozen=True, slots=True)
class CanaryPlan:
    """Canary relation between baseline and candidate endpoints."""

    canary_id: str
    baseline_endpoint_id: str
    candidate_endpoint_id: str
    percentage: int
    policy_ref: str
    evidence_ids: tuple[str, ...]
    created_at_utc: str

    def __post_init__(self) -> None:
        _require_non_empty(self.canary_id, "canary_id")
        _require_non_empty(self.baseline_endpoint_id, "baseline_endpoint_id")
        _require_non_empty(self.candidate_endpoint_id, "candidate_endpoint_id")
        if self.percentage <= 0 or self.percentage >= 100:
            raise ValueError("percentage must be between 1 and 99")
        _require_non_empty(self.policy_ref, "policy_ref")
        _require_string_tuple(self.evidence_ids, "evidence_ids")
        _require_non_empty(self.created_at_utc, "created_at_utc")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CanaryPlan(canary_id={self.canary_id!r}, baseline_endpoint_id={self.baseline_endpoint_id!r}, candidate_endpoint_id={self.candidate_endpoint_id!r})"


@dataclass(frozen=True, slots=True)
class TrafficSplit:
    """Explicit traffic split for one alias."""

    split_id: str
    alias: str
    endpoint_weights: dict[str, int]
    policy_ref: str
    evidence_ids: tuple[str, ...]
    updated_at_utc: str

    def __post_init__(self) -> None:
        _require_non_empty(self.split_id, "split_id")
        _require_non_empty(self.alias, "alias")
        if not self.endpoint_weights:
            raise ValueError("endpoint_weights must be non-empty")
        if sum(self.endpoint_weights.values()) != 100:
            raise ValueError("endpoint weights must sum to 100")
        if any(weight < 0 for weight in self.endpoint_weights.values()):
            raise ValueError("endpoint weights must be non-negative")
        _require_non_empty(self.policy_ref, "policy_ref")
        _require_string_tuple(self.evidence_ids, "evidence_ids")
        _require_non_empty(self.updated_at_utc, "updated_at_utc")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"TrafficSplit(split_id={self.split_id!r}, alias={self.alias!r}, endpoint_weights={self.endpoint_weights!r})"


@dataclass(frozen=True, slots=True)
class MonitorSnapshot:
    """Health, cost, latency, quality, and safety monitor state."""

    endpoint_id: str
    captured_at_utc: str
    statuses: dict[str, str]
    metrics: dict[str, float]
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.endpoint_id, "endpoint_id")
        _require_non_empty(self.captured_at_utc, "captured_at_utc")
        missing = _REQUIRED_MONITORS.difference(self.statuses)
        if missing:
            raise ValueError(f"statuses missing monitor categories: {sorted(missing)}")
        if any(not str(value).strip() for value in self.statuses.values()):
            raise ValueError("monitor statuses must be non-empty")
        if not self.metrics:
            raise ValueError("metrics must be non-empty")
        _require_string_tuple(self.evidence_ids, "evidence_ids")

    @property
    def healthy(self) -> bool:
        """Return true only when every required monitor is explicitly pass."""
        return all(self.statuses[name] == "pass" for name in _REQUIRED_MONITORS)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MonitorSnapshot(endpoint_id={self.endpoint_id!r}, captured_at_utc={self.captured_at_utc!r}, statuses={self.statuses!r})"


@dataclass(frozen=True, slots=True)
class ServingSnapshot:
    """Current serving projection rebuilt from the append-only log."""

    endpoints: tuple[EndpointBinding, ...]
    canaries: tuple[CanaryPlan, ...]
    splits: tuple[TrafficSplit, ...]
    monitors: tuple[MonitorSnapshot, ...]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ServingSnapshot(endpoints={self.endpoints!r}, canaries={self.canaries!r}, splits={self.splits!r})"


class ModelServingLifecycle:
    """Append-only serving lifecycle for registry-backed model versions."""

    def __init__(
        self,
        registry: WorkbenchModelRegistry,
        state_dir: Path | None = None,
        *,
        monitor_limit: int = 1_000,
    ) -> None:
        self._registry = registry
        self._state_dir = Path(state_dir) if state_dir is not None else _DEFAULT_SERVING_DIR
        self._state_path = self._state_dir / _STATE_FILENAME
        self._lock = threading.RLock()
        self._endpoints: dict[str, EndpointBinding] = {}
        self._canaries: dict[str, CanaryPlan] = {}
        self._splits: dict[str, TrafficSplit] = {}
        self._monitors: BoundedList[MonitorSnapshot] = BoundedList(max(1, monitor_limit))
        self._load()

    @property
    def state_path(self) -> Path:
        """Return append-only serving state path."""
        return self._state_path

    def snapshot(self) -> ServingSnapshot:
        """Return current serving projection.

        Returns:
            ServingSnapshot value produced by snapshot().
        """
        with self._lock:
            return ServingSnapshot(
                endpoints=tuple(self._endpoints.values()),
                canaries=tuple(self._canaries.values()),
                splits=tuple(self._splits.values()),
                monitors=tuple(self._monitors),
            )

    def bind_endpoint(self, binding: EndpointBinding) -> EndpointBinding:
        """Append an endpoint binding after registry and evidence checks.

        Returns:
            EndpointBinding value produced by bind_endpoint().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        with self._lock:
            try:
                version = self._registry.get_version(binding.version_id)
            except WorkbenchModelRegistryError as exc:
                raise ServingLifecycleError(
                    "endpoint version is not registered", blockers=(binding.version_id,)
                ) from exc
            _validate_binding_stage(binding, version.stage)
            if binding.endpoint_id in self._endpoints:
                raise ServingLifecycleError("duplicate endpoint binding", blockers=(binding.endpoint_id,))
            self._append_event("endpoint", binding)
            self._endpoints[binding.endpoint_id] = binding
            return binding

    def create_canary(self, canary: CanaryPlan) -> CanaryPlan:
        """Append a canary plan that references existing endpoint bindings.

        Returns:
            Newly constructed canary value.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        with self._lock:
            if canary.baseline_endpoint_id not in self._endpoints:
                raise ServingLifecycleError("baseline endpoint missing", blockers=(canary.baseline_endpoint_id,))
            if canary.candidate_endpoint_id not in self._endpoints:
                raise ServingLifecycleError("candidate endpoint missing", blockers=(canary.candidate_endpoint_id,))
            if canary.canary_id in self._canaries:
                raise ServingLifecycleError("duplicate canary", blockers=(canary.canary_id,))
            self._append_event("canary", canary)
            self._canaries[canary.canary_id] = canary
            return canary

    def apply_traffic_split(self, split: TrafficSplit) -> TrafficSplit:
        """Append a traffic split after all target endpoints are reachable.

        Returns:
            TrafficSplit value produced by apply_traffic_split().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        with self._lock:
            missing = tuple(endpoint_id for endpoint_id in split.endpoint_weights if endpoint_id not in self._endpoints)
            if missing:
                raise ServingLifecycleError("traffic split references missing endpoint", blockers=missing)
            self._append_event("traffic_split", split)
            self._splits[split.split_id] = split
            return split

    def record_monitor_snapshot(self, snapshot: MonitorSnapshot) -> MonitorSnapshot:
        """Append monitor evidence for a known endpoint.

        Returns:
            Outcome produced by record_monitor_snapshot().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        with self._lock:
            if snapshot.endpoint_id not in self._endpoints:
                raise ServingLifecycleError("monitor endpoint missing", blockers=(snapshot.endpoint_id,))
            self._append_event("monitor", snapshot)
            self._append_monitor(snapshot)
            return snapshot

    def latest_monitor(self, endpoint_id: str) -> MonitorSnapshot:
        """Return the latest monitor snapshot for one endpoint or fail closed.

        Returns:
            MonitorSnapshot value produced by latest_monitor().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        for snapshot in reversed(tuple(self._monitors)):
            if snapshot.endpoint_id == endpoint_id:
                return snapshot
        raise ServingLifecycleError("endpoint has no monitor snapshot", blockers=(endpoint_id,))

    def canary_ready(self, canary_id: str) -> bool:
        """Return true only when candidate and baseline monitors both pass.

        Returns:
            bool value produced by canary_ready().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        canary = self._canaries.get(canary_id)
        if canary is None:
            raise ServingLifecycleError("canary missing", blockers=(canary_id,))
        baseline = self.latest_monitor(canary.baseline_endpoint_id)
        candidate = self.latest_monitor(canary.candidate_endpoint_id)
        return baseline.healthy and candidate.healthy

    def _load(self) -> None:
        with self._lock:
            try:
                self._state_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ServingLifecycleError("serving state directory unavailable", path=self._state_dir) from exc
            if not self._state_path.exists():
                self._state_path.touch()
            try:
                raw = self._state_path.read_bytes()
            except OSError as exc:
                raise ServingLifecycleError("serving state unreadable", path=self._state_path) from exc
            if raw and not raw.endswith(b"\n"):
                raise ServingLifecycleError("serving state truncated", path=self._state_path)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ServingLifecycleError("serving state is not UTF-8", path=self._state_path) from exc
            for lineno, line in enumerate(text.splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    self._apply_event(str(row["kind"]), row["payload"])
                except (KeyError, TypeError, json.JSONDecodeError, ValueError) as exc:
                    raise ServingLifecycleError(
                        f"serving state damaged at line {lineno}", path=self._state_path
                    ) from exc

    def _append_event(self, kind: str, payload: Any) -> None:
        payload_data = _json_safe(payload)
        if not isinstance(payload_data, dict):
            raise ServingLifecycleError("serving event payload must be an object", path=self._state_path)
        envelope = {"kind": kind, "payload": payload_data}
        line = json.dumps(envelope, separators=(",", ":"), sort_keys=True) + "\n"
        try:
            with self._state_path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
            # spine_consumers invokes get_spine() and absorbs observability failures.
            audit_project_id = str(payload_data.get("project_id") or payload_data.get("scope_project_id") or "default")
            record_asset_written(
                asset_id=str(payload_data.get("endpoint_id") or payload_data.get("model_id") or kind),
                kind="model",
                project_id=audit_project_id,
                path=str(self._state_path),
                redact_fields=["path"],
            )
            if kind in {"slot", "lease", "endpoint"}:
                record_lease_acquired(
                    lease_id=str(payload_data.get("endpoint_id") or kind),
                    resource_kind=kind,
                    project_id=audit_project_id,
                )
        except OSError as exc:
            raise ServingLifecycleError("serving append failed", path=self._state_path) from exc

    def _apply_event(self, kind: str, payload: dict[str, Any]) -> None:
        if kind == "endpoint":
            binding = _endpoint_from_payload(payload)
            self._endpoints[binding.endpoint_id] = binding
        elif kind == "canary":
            canary = _canary_from_payload(payload)
            self._canaries[canary.canary_id] = canary
        elif kind == "traffic_split":
            split = _split_from_payload(payload)
            self._splits[split.split_id] = split
        elif kind == "monitor":
            self._append_monitor(_monitor_from_payload(payload))
        else:
            raise ValueError(f"unknown serving event kind {kind!r}")

    def _append_monitor(self, snapshot: MonitorSnapshot) -> None:
        # _monitors is a BoundedList with maxlen set by monitor_limit.
        self._monitors.append(snapshot)


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


def _require_string_tuple(values: tuple[str, ...], field_name: str) -> None:
    if not isinstance(values, tuple) or not values:
        raise ValueError(f"{field_name} must be a non-empty tuple")
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise ValueError(f"{field_name} must contain non-empty strings")


def _validate_binding_stage(binding: EndpointBinding, stage: ModelStage) -> None:
    allowed = {
        EndpointStatus.CANDIDATE: {ModelStage.CANDIDATE, ModelStage.CANARY, ModelStage.SERVING},
        EndpointStatus.CANARY: {ModelStage.CANARY},
        EndpointStatus.ACTIVE: {ModelStage.SERVING},
        EndpointStatus.DRAINING: {ModelStage.SERVING, ModelStage.DEPRECATED},
        EndpointStatus.DISABLED: {ModelStage.CANDIDATE, ModelStage.CANARY, ModelStage.SERVING, ModelStage.DEPRECATED},
    }[binding.status]
    if stage not in allowed:
        raise ServingLifecycleError(
            "endpoint version stage is not eligible for binding status",
            blockers=(f"{binding.version_id}:{stage.value}->{binding.status.value}",),
        )


def utc_now_iso() -> str:
    """Return an aware UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def _tuple(payload: dict[str, Any], key: str) -> tuple[str, ...]:
    return tuple(str(value) for value in payload.get(key, ()))


def _endpoint_from_payload(payload: dict[str, Any]) -> EndpointBinding:
    return EndpointBinding(
        endpoint_id=str(payload["endpoint_id"]),
        version_id=str(payload["version_id"]),
        alias=str(payload["alias"]),
        runtime_kind=str(payload["runtime_kind"]),
        base_url=str(payload["base_url"]),
        status=EndpointStatus(str(payload["status"])),
        traffic_weight=int(payload["traffic_weight"]),
        policy_ref=str(payload["policy_ref"]),
        evidence_ids=_tuple(payload, "evidence_ids"),
        project_id=str(payload.get("project_id", "default")),
    )


def _canary_from_payload(payload: dict[str, Any]) -> CanaryPlan:
    return CanaryPlan(
        canary_id=str(payload["canary_id"]),
        baseline_endpoint_id=str(payload["baseline_endpoint_id"]),
        candidate_endpoint_id=str(payload["candidate_endpoint_id"]),
        percentage=int(payload["percentage"]),
        policy_ref=str(payload["policy_ref"]),
        evidence_ids=_tuple(payload, "evidence_ids"),
        created_at_utc=str(payload["created_at_utc"]),
    )


def _split_from_payload(payload: dict[str, Any]) -> TrafficSplit:
    return TrafficSplit(
        split_id=str(payload["split_id"]),
        alias=str(payload["alias"]),
        endpoint_weights={str(key): int(value) for key, value in dict(payload["endpoint_weights"]).items()},
        policy_ref=str(payload["policy_ref"]),
        evidence_ids=_tuple(payload, "evidence_ids"),
        updated_at_utc=str(payload["updated_at_utc"]),
    )


def _monitor_from_payload(payload: dict[str, Any]) -> MonitorSnapshot:
    return MonitorSnapshot(
        endpoint_id=str(payload["endpoint_id"]),
        captured_at_utc=str(payload["captured_at_utc"]),
        statuses={str(key): str(value) for key, value in dict(payload["statuses"]).items()},
        metrics={str(key): float(value) for key, value in dict(payload["metrics"]).items()},
        evidence_ids=_tuple(payload, "evidence_ids"),
    )


__all__ = [
    "CanaryPlan",
    "EndpointBinding",
    "EndpointStatus",
    "ModelServingLifecycle",
    "MonitorSnapshot",
    "ServingLifecycleError",
    "ServingSnapshot",
    "TrafficSplit",
    "utc_now_iso",
]
