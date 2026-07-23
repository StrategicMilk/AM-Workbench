"""Step 0: typed file-level data-asset records grouped by a dataset revision.

Read-only value objects; no I/O. Used by
``vetinari.workbench.dataset_revisions.DatasetRevision.assets``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class DataAssetKind(str, Enum):
    """Kinds of file-level data assets grouped into dataset revisions."""

    FILE = "file"
    URI = "uri"
    DATABASE_VIEW = "database_view"
    INLINE = "inline"


@dataclass(frozen=True, slots=True)
class DataAsset:
    """One content-addressed data asset inside a dataset revision."""

    asset_path: str
    kind: DataAssetKind
    content_sha256: str
    size_bytes: int
    mime_type: str
    captured_at_utc: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, DataAssetKind):
            try:
                object.__setattr__(self, "kind", DataAssetKind(getattr(self.kind, "value", self.kind)))
            except ValueError as exc:
                raise ValueError("DataAsset.kind must be a DataAssetKind") from exc
        if not self.asset_path or not self.asset_path.strip():
            raise ValueError("DataAsset.asset_path must be non-empty")
        if not _SHA256_RE.fullmatch(self.content_sha256):
            raise ValueError("DataAsset.content_sha256 must be a 64-character lowercase hex digest")
        if self.size_bytes < 0:
            raise ValueError("DataAsset.size_bytes must be non-negative")
        if not self.mime_type or not self.mime_type.strip():
            raise ValueError("DataAsset.mime_type must be non-empty")
        if not self.captured_at_utc or not self.captured_at_utc.strip():
            raise ValueError("DataAsset.captured_at_utc must be non-empty")

    def __repr__(self) -> str:
        return (
            "DataAsset("
            f"asset_path={self.asset_path!r}, kind={self.kind.value!r}, "
            f"content_sha256={self.content_sha256[:8]!r}, size_bytes={self.size_bytes!r})"
        )


__all__ = ["DataAsset", "DataAssetKind"]
