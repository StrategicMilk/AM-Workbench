"""Block-on-failure policy for artifact-review lint findings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .lints import LintCatalogError
from .runtime import ArtifactReviewLintFinding, LintSeverity

_SEVERITY_ORDER = {
    LintSeverity.INFO: 0,
    LintSeverity.WARNING: 1,
    LintSeverity.ERROR: 2,
    LintSeverity.BLOCKER: 3,
}


@dataclass(frozen=True, slots=True)
class ArtifactLintPolicy:
    """Configured threshold for blocking artifact promotion."""

    block_on_severity: LintSeverity
    per_kind_overrides: tuple[tuple[str, LintSeverity], ...] = ()

    @classmethod
    def from_catalog_data(cls, data: dict[str, Any]) -> ArtifactLintPolicy:
        """Build policy from the catalog policy mapping.

        Returns:
            ArtifactLintPolicy value produced by from_catalog_data().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        threshold = _parse_severity(data.get("block_on_severity"))
        overrides = data.get("per_kind_overrides", {})
        if not isinstance(overrides, dict):
            raise LintCatalogError("per_kind_overrides must be a mapping")
        parsed = []
        for kind, row in overrides.items():
            if not isinstance(kind, str) or not isinstance(row, dict):
                raise LintCatalogError("per-kind overrides must map kind to mapping")
            parsed.append((kind, _parse_severity(row.get("block_on_severity"))))
        return cls(threshold, tuple(sorted(parsed)))

    def is_blocking(self, finding: ArtifactReviewLintFinding, *, kind: str) -> bool:
        """Return true when this finding blocks the artifact kind.

        Returns:
            Boolean indicating whether is blocking.
        """
        threshold = dict(self.per_kind_overrides).get(kind, self.block_on_severity)
        return _SEVERITY_ORDER[finding.severity] >= _SEVERITY_ORDER[threshold]


def _parse_severity(value: Any) -> LintSeverity:
    if not isinstance(value, str):
        raise LintCatalogError("block_on_severity must be a string")
    try:
        return LintSeverity[value]
    except KeyError as exc:
        raise LintCatalogError(f"unknown block_on_severity {value!r}") from exc


__all__ = ["ArtifactLintPolicy"]
