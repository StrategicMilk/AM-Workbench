"""Typed manifest records for supervised Python workers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SUPPORTED_TYPES = frozenset({"str", "int", "float", "bool", "dict", "list"})
_SUPPORTED_RESOURCES = frozenset({"cpu", "memory_mb", "gpu_vram_mb", "disk_mb", "network"})


class WorkerManifestError(ValueError):
    """Raised when a worker manifest cannot be trusted."""


class CrashRecoveryPolicy(str, Enum):
    """Crash handling modes workers must declare before execution."""

    RETRY_ONCE = "retry_once"
    MARK_FAILED = "mark_failed"
    ROLLBACK_AND_MARK_FAILED = "rollback_and_mark_failed"


@dataclass(frozen=True, slots=True)
class WorkerIOField:
    """One typed input or output field in a worker manifest."""

    name: str
    type_name: str
    required: bool = True

    def __post_init__(self) -> None:
        _require_identifier(self.name, "WorkerIOField.name")
        if self.type_name not in _SUPPORTED_TYPES:
            raise WorkerManifestError(f"unsupported IO type {self.type_name!r} for field {self.name!r}")


@dataclass(frozen=True, slots=True)
class ResourceDeclaration:
    """One resource bound required by a Python worker."""

    name: str
    amount: int

    def __post_init__(self) -> None:
        if self.name not in _SUPPORTED_RESOURCES:
            raise WorkerManifestError(f"unsupported resource declaration {self.name!r}")
        if self.amount < 0:
            raise WorkerManifestError(f"resource declaration {self.name!r} cannot be negative")


@dataclass(frozen=True, slots=True)
class WorkerManifest:
    """Complete supervised-worker manifest."""

    worker_id: str
    version: str
    inputs: tuple[WorkerIOField, ...]
    outputs: tuple[WorkerIOField, ...]
    resources: tuple[ResourceDeclaration, ...]
    recovery: CrashRecoveryPolicy
    receipt_subject: str
    allow_network: bool = False

    def __post_init__(self) -> None:
        _require_identifier(self.worker_id, "WorkerManifest.worker_id")
        _require_non_empty(self.version, "WorkerManifest.version")
        _require_identifier(self.receipt_subject, "WorkerManifest.receipt_subject")
        object.__setattr__(self, "recovery", CrashRecoveryPolicy(getattr(self.recovery, "value", self.recovery)))
        if not self.inputs:
            raise WorkerManifestError("worker manifest must declare at least one input")
        if not self.outputs:
            raise WorkerManifestError("worker manifest must declare at least one output")
        _require_unique_fields(self.inputs, "inputs")
        _require_unique_fields(self.outputs, "outputs")
        resources_by_name = {resource.name: resource for resource in self.resources}
        if "network" in resources_by_name and resources_by_name["network"].amount and not self.allow_network:
            raise WorkerManifestError("network resource requires allow_network=True")

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> WorkerManifest:
        """Build a manifest from a JSON-compatible payload."""
        try:
            return cls(
                worker_id=str(payload["worker_id"]),
                version=str(payload["version"]),
                inputs=tuple(WorkerIOField(**row) for row in payload["inputs"]),
                outputs=tuple(WorkerIOField(**row) for row in payload["outputs"]),
                resources=tuple(ResourceDeclaration(**row) for row in payload.get("resources", ())),
                recovery=CrashRecoveryPolicy(payload["recovery"]),
                receipt_subject=str(payload["receipt_subject"]),
                allow_network=bool(payload.get("allow_network")),
            )
        except KeyError as exc:
            raise WorkerManifestError(f"worker manifest missing required field {exc.args[0]!r}") from exc


def expected_python_type(type_name: str) -> type[Any]:
    """Return the Python runtime type for a manifest type name."""
    mapping: dict[str, type[Any]] = {
        "str": str,
        "int": int,
        "float": float,
        "bool": bool,
        "dict": dict,
        "list": list,
    }
    return mapping[type_name]


def _require_non_empty(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise WorkerManifestError(f"{field_name} must be non-empty")


def _require_identifier(value: str, field_name: str) -> None:
    if not value or not _ID_RE.fullmatch(value) or ".." in value:
        raise WorkerManifestError(f"{field_name} {value!r} fails identifier/path-traversal validation")


def _require_unique_fields(fields: tuple[WorkerIOField, ...], label: str) -> None:
    names = [field.name for field in fields]
    if len(names) != len(set(names)):
        raise WorkerManifestError(f"worker manifest {label} contain duplicate field names")
