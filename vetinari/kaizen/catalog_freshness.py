"""Catalog freshness evaluation for model catalog sources."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from vetinari.agents.contracts import OutcomeSignal, ToolEvidence
from vetinari.types import EvidenceBasis

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CatalogFreshnessReport:
    """Freshness result for catalog YAML files."""

    oldest_verified_on: date | None
    newest_verified_on: date | None
    entries_stale_warning: tuple[str, ...]
    entries_stale_fail: tuple[str, ...]
    entries_missing_sources: tuple[str, ...]
    entries_with_dead_links: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return (
            "CatalogFreshnessReport("
            f"oldest_verified_on={self.oldest_verified_on!r}, "
            f"newest_verified_on={self.newest_verified_on!r}, "
            f"stale_fail={len(self.entries_stale_fail)}, "
            f"missing_sources={len(self.entries_missing_sources)}, "
            f"dead_links={len(self.entries_with_dead_links)})"
        )

    def to_outcome_signal(self) -> OutcomeSignal:
        """Convert the report to an OutcomeSignal.

        Returns:
            OutcomeSignal that fails when stale, missing-source, or dead-link
            entries are present.
        """
        issues = self.entries_stale_fail + self.entries_missing_sources + self.entries_with_dead_links
        evidence_summary = (
            f"oldest={self.oldest_verified_on}; newest={self.newest_verified_on}; "
            f"stale_fail={len(self.entries_stale_fail)}; "
            f"missing_sources={len(self.entries_missing_sources)}; "
            f"dead_links={len(self.entries_with_dead_links)}"
        )
        return OutcomeSignal(
            passed=not issues,
            score=1.0 if not issues else 0.0,
            basis=EvidenceBasis.TOOL_EVIDENCE,
            tool_evidence=(
                ToolEvidence(
                    tool_name="catalog_freshness",
                    command="evaluate_catalog_freshness",
                    exit_code=0 if not issues else 1,
                    stdout_snippet=evidence_summary,
                    passed=not issues,
                ),
            ),
            issues=tuple(issues),
        )


def _walk_dates(value: Any, prefix: str = "") -> list[tuple[str, date]]:
    found: list[tuple[str, date]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            if key in {"verified_on", "retrieved_on", "last_validated_utc"}:
                try:
                    found.append((child_prefix, date.fromisoformat(str(child).split("T")[0])))
                except ValueError as exc:
                    logger.debug("Ignoring unparsable catalog date at %s: %s", child_prefix, exc)
                    continue
            found.extend(_walk_dates(child, child_prefix))
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            found.extend(_walk_dates(child, f"{prefix}[{idx}]"))
    return found


def _has_missing_sources(data: dict[str, Any]) -> bool:
    """Return True if any entry in the dict has a source_url but no fetched_in_session flag.

    Walks the dict once, checking per-entry rather than doing global string search.

    Args:
        data: Parsed YAML data dict.

    Returns:
        True if any entry has ``source_url`` but ``fetched_in_session`` is absent or falsy.
    """
    if isinstance(data, dict):
        if "source_url" in data and not data.get("fetched_in_session"):
            return True
        return any(_has_missing_sources(child) for child in data.values())
    if isinstance(data, list):
        return any(_has_missing_sources(child) for child in data)
    return False


def evaluate_catalog_freshness(catalog_path: str | Path, now: date | None = None) -> CatalogFreshnessReport:
    """Evaluate catalog source dates without making network calls.

    Args:
        catalog_path: Repository root directory containing the config/ tree.
        now: Reference date for staleness calculation (defaults to today UTC).

    Returns:
        CatalogFreshnessReport describing freshness status of all catalog files.
    """
    root = Path(catalog_path)
    now_date = now or datetime.now(timezone.utc).date()
    dates: list[tuple[str, date]] = []
    missing_sources: list[str] = []
    for rel in [
        "config/knowledge/benchmarks.yaml",
        "config/knowledge/model_families.yaml",
        "config/quantization_recommendations.yaml",
        "config/models.yaml",
    ]:
        path = root / rel
        if not path.exists():
            continue
        # Parse once; walk the dict for dates and missing-source detection
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        dates.extend((f"{rel}:{name}", value) for name, value in _walk_dates(data))
        if _has_missing_sources(data):
            missing_sources.append(rel)
    stale_warning = tuple(name for name, seen in dates if (now_date - seen).days > 30)
    stale_fail = tuple(name for name, seen in dates if (now_date - seen).days > 90)
    values = [seen for _, seen in dates]
    return CatalogFreshnessReport(
        oldest_verified_on=min(values) if values else None,
        newest_verified_on=max(values) if values else None,
        entries_stale_warning=stale_warning,
        entries_stale_fail=stale_fail,
        entries_missing_sources=tuple(missing_sources),
    )
