"""Minimum data-provenance record for training-ledger entries."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class LicenseClass(str, Enum):
    """Training data license classification."""

    PERMISSIVE = "PERMISSIVE"
    PERMISSIVE_WITH_ATTRIBUTION = "PERMISSIVE_WITH_ATTRIBUTION"
    COPYLEFT = "COPYLEFT"
    NON_COMMERCIAL = "NON_COMMERCIAL"
    RESTRICTED = "RESTRICTED"
    PROPRIETARY_OWNED = "PROPRIETARY_OWNED"
    PROPRIETARY_LICENSED = "PROPRIETARY_LICENSED"
    UNKNOWN = "UNKNOWN"


class ContaminationStatus(str, Enum):
    """Known evaluation contamination state for training data."""

    UNCHECKED = "UNCHECKED"
    CHECKED_CLEAN = "CHECKED_CLEAN"
    CHECKED_KNOWN_CONTAMINATED = "CHECKED_KNOWN_CONTAMINATED"
    CHECKED_SUSPECTED = "CHECKED_SUSPECTED"


class RedactionStatus(str, Enum):
    """PII or sensitive-data redaction state."""

    NONE = "NONE"
    PII_REDACTED = "PII_REDACTED"
    FULL_REDACT = "FULL_REDACT"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class UnknownDatasetRevisionError(ValueError):
    """Raised when a dataset_revision_id supplied to the helper is invalid."""


@dataclass(frozen=True, slots=True)
class TrainingDataProvenance:
    """Minimal provenance fields required for every training ledger entry."""

    source: str
    license_classification: LicenseClass
    contamination_status: ContaminationStatus
    redaction_status: RedactionStatus
    dataset_revision_id: str | None = None
    contamination_check_ref: str | None = None

    def __repr__(self) -> str:
        """Return a compact debug representation keyed by source and classifications."""
        base = (
            "TrainingDataProvenance("
            f"source={self.source!r}, license={self.license_classification.value!r}, "
            f"contamination={self.contamination_status.value!r}, redaction={self.redaction_status.value!r}"
        )
        if self.dataset_revision_id is not None:
            base += f", dataset_revision_id={self.dataset_revision_id!r}"
        return base + ")"

    def __post_init__(self) -> None:
        if not self.source or not self.source.strip():
            raise ValueError("TrainingDataProvenance.source must be a non-empty string")
        if not isinstance(self.license_classification, LicenseClass):
            raise ValueError("license_classification is required")
        if not isinstance(self.contamination_status, ContaminationStatus):
            raise ValueError("contamination_status is required")
        if not isinstance(self.redaction_status, RedactionStatus):
            raise ValueError("redaction_status is required")
        if self.dataset_revision_id is not None and (
            not isinstance(self.dataset_revision_id, str) or not self.dataset_revision_id.strip()
        ):
            raise UnknownDatasetRevisionError(
                f"dataset_revision_id must be None or a non-empty string, got {self.dataset_revision_id!r}"
            )
        if self.contamination_check_ref is not None and (
            not isinstance(self.contamination_check_ref, str) or not self.contamination_check_ref.strip()
        ):
            raise ValueError("contamination_check_ref must be None or a non-empty string")

    def with_dataset_revision(self, revision_id: str) -> TrainingDataProvenance:
        """Return a new TrainingDataProvenance with dataset_revision_id set.

        Returns:
            TrainingDataProvenance value produced by with_dataset_revision().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        from vetinari.workbench.dataset_revisions import _REVISION_ID_RE

        if not revision_id or not revision_id.strip():
            raise UnknownDatasetRevisionError(f"dataset_revision_id must be a non-empty string, got {revision_id!r}")
        if not _REVISION_ID_RE.fullmatch(revision_id):
            raise UnknownDatasetRevisionError(
                f"dataset_revision_id {revision_id!r} fails revision-id regex (path traversal rejected)"
            )
        return TrainingDataProvenance(
            source=self.source,
            license_classification=self.license_classification,
            contamination_status=self.contamination_status,
            redaction_status=self.redaction_status,
            dataset_revision_id=revision_id,
            contamination_check_ref=self.contamination_check_ref,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation.

        Returns:
            Dictionary suitable for ledger persistence.
        """
        data = asdict(self)
        data["license_classification"] = self.license_classification.value
        data["contamination_status"] = self.contamination_status.value
        data["redaction_status"] = self.redaction_status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrainingDataProvenance:
        """Build a provenance record from serialized data.

        Args:
            data: Serialized provenance fields.

        Returns:
            Reconstructed TrainingDataProvenance.

        Raises:
            ValueError: If required fields are missing or enum values are invalid.
        """
        missing = [
            key
            for key in ("source", "license_classification", "contamination_status", "redaction_status")
            if key not in data
        ]
        if missing:
            raise ValueError(f"missing provenance field: {missing[0]}")
        return cls(
            source=data["source"],
            license_classification=LicenseClass(data["license_classification"]),
            contamination_status=ContaminationStatus(data["contamination_status"]),
            redaction_status=RedactionStatus(data["redaction_status"]),
            dataset_revision_id=data.get("dataset_revision_id"),
            contamination_check_ref=data.get("contamination_check_ref"),
        )


# Backward-compatible public alias; the concrete class name remains distinct
# from vetinari.agents.skill_contract.DataProvenance to avoid same-name drift.
DataProvenance = TrainingDataProvenance
