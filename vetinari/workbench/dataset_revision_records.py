"""Typed dataset revision records and JSON payload helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from vetinari.workbench.assets import AssetTaint
from vetinari.workbench.data_assets import DataAsset

_REVISION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,127}$")
_TAG_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class RevisionStatus(str, Enum):
    """Review lifecycle status for a dataset revision."""

    OPEN = "open"
    REVIEWED = "reviewed"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


class DatasetRemoteKind(str, Enum):
    """Remote adapter kinds supported by the revision store contract."""

    LOCAL = "local"
    DVC = "dvc"
    LAKEFS = "lakefs"
    LANCE = "lance"


class DatasetRevisionError(Exception):
    """Base exception for dataset revision store failures."""


class DatasetRevisionUnknown(DatasetRevisionError):
    """Raised when a branch, tag, parent, or revision id is absent."""


class DatasetRevisionAuthFailed(DatasetRevisionError):
    """Raised when a configured remote refuses authentication."""


class DatasetRevisionSchemaMismatch(DatasetRevisionError):
    """Raised when durable revision state cannot be trusted."""


class DatasetRevisionRemoteUnavailable(DatasetRevisionError):
    """Raised when a non-local remote adapter is unavailable."""


@dataclass(frozen=True, slots=True)
class DatasetBranch:
    """A branch name and its current dataset revision head."""

    name: str
    head_revision_id: str
    created_at_utc: str
    created_by: str

    def __post_init__(self) -> None:
        _validate_identifier(self.name, _BRANCH_NAME_RE, "DatasetBranch.name")
        _validate_identifier(self.head_revision_id, _REVISION_ID_RE, "DatasetBranch.head_revision_id")
        _require_non_empty(self.created_at_utc, "DatasetBranch.created_at_utc")
        _require_non_empty(self.created_by, "DatasetBranch.created_by")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DatasetBranch(name={self.name!r}, head_revision_id={self.head_revision_id!r}, created_at_utc={self.created_at_utc!r})"


@dataclass(frozen=True, slots=True)
class DatasetTag:
    """A stable tag pointer to a dataset revision."""

    name: str
    revision_id: str
    created_at_utc: str
    created_by: str
    message: str = ""

    def __post_init__(self) -> None:
        _validate_identifier(self.name, _TAG_NAME_RE, "DatasetTag.name")
        _validate_identifier(self.revision_id, _REVISION_ID_RE, "DatasetTag.revision_id")
        _require_non_empty(self.created_at_utc, "DatasetTag.created_at_utc")
        _require_non_empty(self.created_by, "DatasetTag.created_by")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"DatasetTag(name={self.name!r}, revision_id={self.revision_id!r}, created_at_utc={self.created_at_utc!r})"
        )


@dataclass(frozen=True, slots=True)
class DatasetDiff:
    """Added, removed, and changed data assets between two revisions."""

    parent_revision_id: str
    child_revision_id: str
    added_assets: tuple[DataAsset, ...] = ()
    removed_assets: tuple[DataAsset, ...] = ()
    changed_assets: tuple[tuple[DataAsset, DataAsset], ...] = ()

    def __post_init__(self) -> None:
        _validate_identifier(self.parent_revision_id, _REVISION_ID_RE, "DatasetDiff.parent_revision_id")
        _validate_identifier(self.child_revision_id, _REVISION_ID_RE, "DatasetDiff.child_revision_id")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DatasetDiff(parent_revision_id={self.parent_revision_id!r}, child_revision_id={self.child_revision_id!r}, added_assets={self.added_assets!r})"


@dataclass(frozen=True, slots=True)
class RevisionGateResult:
    """Fail-closed promotion gate result for a dataset revision."""

    passed: bool
    reasons: tuple[str, ...] = ("unreviewed",)

    def __post_init__(self) -> None:
        if self.passed and not self.reasons:
            raise ValueError("RevisionGateResult.passed=True requires at least one reason")
        for reason in self.reasons:
            _require_non_empty(reason, "RevisionGateResult.reasons[]")


@dataclass(frozen=True, slots=True)
class DatasetRevision:
    """Immutable dataset revision containing a tuple of file-level assets."""

    revision_id: str
    parent_revision_id: str | None
    branch: str
    status: RevisionStatus
    assets: tuple[DataAsset, ...]
    created_at_utc: str
    source_receipt_ids: tuple[str, ...]
    reviewer_ids: tuple[str, ...] = ()
    message: str = ""
    taints: tuple[AssetTaint, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", RevisionStatus(getattr(self.status, "value", self.status)))
        coerced_assets: list[DataAsset] = []
        for asset in self.assets:
            if isinstance(asset, DataAsset):
                coerced_assets.append(asset)
            elif is_dataclass(asset) and not isinstance(asset, type):
                values = {field.name: getattr(asset, field.name) for field in fields(asset)}
                coerced_assets.append(DataAsset(**values))
            else:
                raise ValueError("DatasetRevision.assets must contain DataAsset instances")
        object.__setattr__(self, "assets", tuple(coerced_assets))
        _validate_identifier(self.revision_id, _REVISION_ID_RE, "DatasetRevision.revision_id")
        if self.parent_revision_id is not None:
            _validate_identifier(
                self.parent_revision_id,
                _REVISION_ID_RE,
                "DatasetRevision.parent_revision_id",
            )
        _validate_identifier(self.branch, _BRANCH_NAME_RE, "DatasetRevision.branch")
        _require_non_empty(self.created_at_utc, "DatasetRevision.created_at_utc")
        _require_non_empty(self.message, "DatasetRevision.message")
        for receipt_id in self.source_receipt_ids:
            _require_non_empty(receipt_id, "DatasetRevision.source_receipt_ids[]")
        if self.status.value == RevisionStatus.PROMOTED.value and not self.source_receipt_ids:
            raise ValueError("DatasetRevision.PROMOTED requires at least one source receipt")
        if (
            self.status.value in {RevisionStatus.REVIEWED.value, RevisionStatus.PROMOTED.value}
            and not self.reviewer_ids
        ):
            raise ValueError("DatasetRevision reviewed/promoted statuses require reviewer_ids")
        for reviewer_id in self.reviewer_ids:
            _require_non_empty(reviewer_id, "DatasetRevision.reviewer_ids[]")
        for taint in self.taints:
            if not isinstance(taint, AssetTaint):
                raise ValueError("DatasetRevision.taints must contain AssetTaint instances")

    def __repr__(self) -> str:
        return (
            "DatasetRevision("
            f"revision_id={self.revision_id!r}, branch={self.branch!r}, "
            f"status={self.status.value!r}, assets={len(self.assets)!r}, "
            f"parent_revision_id={self.parent_revision_id!r})"
        )


def _require_non_empty(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


def _validate_identifier(value: str, pattern: re.Pattern[str], field_name: str) -> None:
    if not value or not pattern.fullmatch(value):
        raise ValueError(f"{field_name} {value!r} fails path-traversal regex")
    if ".." in value or ("/" in value and pattern is not _BRANCH_NAME_RE):
        raise ValueError(f"{field_name} {value!r} fails path-traversal regex")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {field.name: _to_jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    return value


def _revision_from_payload(payload: dict[str, Any]) -> DatasetRevision:
    return DatasetRevision(
        revision_id=payload["revision_id"],
        parent_revision_id=payload.get("parent_revision_id"),
        branch=payload["branch"],
        status=RevisionStatus(payload["status"]),
        assets=tuple(_data_asset_from_payload(row) for row in payload.get("assets", ())),
        created_at_utc=payload["created_at_utc"],
        source_receipt_ids=tuple(payload.get("source_receipt_ids", ())),
        reviewer_ids=tuple(payload.get("reviewer_ids", ())),
        message=payload["message"],
        taints=tuple(_asset_taint_from_payload(row) for row in payload.get("taints", ())),
    )


def _branch_from_payload(payload: dict[str, Any]) -> DatasetBranch:
    return DatasetBranch(
        name=payload["name"],
        head_revision_id=payload["head_revision_id"],
        created_at_utc=payload["created_at_utc"],
        created_by=payload["created_by"],
    )


def _tag_from_payload(payload: dict[str, Any]) -> DatasetTag:
    return DatasetTag(
        name=payload["name"],
        revision_id=payload["revision_id"],
        created_at_utc=payload["created_at_utc"],
        created_by=payload["created_by"],
        message=payload.get("message", ""),
    )


def _data_asset_from_payload(payload: dict[str, Any]) -> DataAsset:
    from vetinari.workbench.data_assets import DataAssetKind

    return DataAsset(
        asset_path=payload["asset_path"],
        kind=DataAssetKind(payload["kind"]),
        content_sha256=payload["content_sha256"],
        size_bytes=int(payload["size_bytes"]),
        mime_type=payload["mime_type"],
        captured_at_utc=payload["captured_at_utc"],
    )


def _asset_taint_from_payload(payload: dict[str, Any]) -> AssetTaint:
    return AssetTaint(
        taint_id=payload["taint_id"],
        severity=payload["severity"],
        reason=payload["reason"],
        attached_at_utc=payload["attached_at_utc"],
    )
