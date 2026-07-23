"""Workbench status config loading, aggregation, and assistant projection."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from vetinari.workbench.status.contracts import (
    WorkbenchHealthDomain,
    WorkbenchHealthResult,
    WorkbenchHealthState,
    WorkbenchStatusConfig,
    WorkbenchStatusSeverity,
    WorkbenchStatusSnapshot,
)
from vetinari.workbench.status.probes import normalize_probe_results

SCHEMA_VERSION = "1.0"
DEFAULT_CONFIG_PATH = Path("config") / "workbench" / "status_checks.yaml"
REQUIRED_DOMAINS: tuple[WorkbenchHealthDomain, ...] = tuple(WorkbenchHealthDomain)
_SECRET_PATTERN = re.compile(r"(?i)(api[_-]?key|token|secret|password|credential|bearer)[=: _-]*[A-Za-z0-9._~+/=-]{4,}")
_SECRET_KEY_PATTERN = re.compile(r"(?i)(api[_-]?key|token|secret|password|credential|authorization)")
_SECRET_VALUE_PATTERN = re.compile(
    r"(?i)\b(bearer\s+[A-Za-z0-9._~+/=-]{8,}|sk-[A-Za-z0-9]{8,}|gh[pousr]_[A-Za-z0-9_]{8,}|AKIA[0-9A-Z]{12,})\b"
)


class WorkbenchStatusConfigError(RuntimeError):
    """Raised when status config is missing, unreadable, or invalid."""


def load_workbench_status_config(path: str | Path = DEFAULT_CONFIG_PATH) -> WorkbenchStatusConfig:
    """Load static Workbench status config and validate required domains.

    Returns:
        Resolved workbench status config value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise WorkbenchStatusConfigError(f"status config unreadable: {exc}") from exc
    except yaml.YAMLError as exc:
        raise WorkbenchStatusConfigError(f"status config corrupt: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise WorkbenchStatusConfigError("status config root must be a mapping")
    if str(raw.get("schema_version")) != SCHEMA_VERSION:
        raise WorkbenchStatusConfigError(f"status config schema_version must be {SCHEMA_VERSION}")
    domains_raw = raw.get("domains")
    if not isinstance(domains_raw, Mapping):
        raise WorkbenchStatusConfigError("status config domains must be a mapping")
    domains = tuple(WorkbenchHealthDomain(str(name)) for name in domains_raw)
    missing = [domain.value for domain in REQUIRED_DOMAINS if domain not in domains]
    if missing:
        raise WorkbenchStatusConfigError(f"status config missing required domains: {', '.join(missing)}")
    settings_targets: dict[WorkbenchHealthDomain, str] = {}
    fix_actions: dict[WorkbenchHealthDomain, str] = {}
    for domain in REQUIRED_DOMAINS:
        entry = domains_raw.get(domain.value)
        if not isinstance(entry, Mapping):
            raise WorkbenchStatusConfigError(f"status config domain {domain.value} must be a mapping")
        target = str(entry.get("settings_target", "")).strip()
        action = str(entry.get("fix_action", "")).strip()
        informational = bool(entry.get("informational", False))
        if not informational and not (target or action):
            raise WorkbenchStatusConfigError(f"status config domain {domain.value} needs settings_target or fix_action")
        if target:
            settings_targets[domain] = target
        if action:
            fix_actions[domain] = action
    return WorkbenchStatusConfig(
        schema_version=SCHEMA_VERSION,
        required_domains=REQUIRED_DOMAINS,
        settings_targets=settings_targets,
        fix_actions=fix_actions,
    )


def build_workbench_status_snapshot(
    *,
    project_id: str = "default",
    dependency_snapshots: Mapping[str, Any] | None = None,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    now: datetime | None = None,
) -> WorkbenchStatusSnapshot:
    """Build a read-only Workbench status snapshot from configured probes.

    Returns:
        Newly constructed workbench status snapshot value.
    """
    generated_at = _iso(now)
    try:
        config = load_workbench_status_config(config_path)
        results = normalize_probe_results(dependency_snapshots, config=config, now=now)
    except Exception as exc:
        config = _fallback_config()
        results = tuple(
            WorkbenchHealthResult(
                domain=domain,
                key=f"{domain.value}.config",
                state=WorkbenchHealthState.BROKEN,
                severity=WorkbenchStatusSeverity.ERROR,
                summary=f"status config unavailable; failed closed: {type(exc).__name__}",
                evidence_refs=(f"status-config:{config_path}",),
                checked_at_utc=generated_at,
                settings_target=config.settings_targets.get(domain),
                fix_action=config.fix_actions.get(domain),
            )
            for domain in REQUIRED_DOMAINS
        )
    counts = _state_counts(results)
    return WorkbenchStatusSnapshot(
        project_id=str(project_id or "default"),
        overall_state=_overall_state(results),
        generated_at_utc=generated_at,
        results=results,
        state_counts=counts,
        config=config,
    )


def build_assistant_status_context(snapshot: WorkbenchStatusSnapshot) -> dict[str, Any]:
    """Return a redacted read-only assistant context projection.

    Returns:
        Newly constructed assistant status context value.
    """
    visible = [result for result in snapshot.results if result.assistant_visible]
    payload = {
        "project_id": snapshot.project_id,
        "overall_state": snapshot.overall_state.value,
        "generated_at_utc": snapshot.generated_at_utc,
        "read_only": True,
        "write_callbacks": [],
        "results": [
            {
                "domain": result.domain.value,
                "key": result.key,
                "state": result.state.value,
                "severity": result.severity.value,
                "summary": result.summary,
                "settings_target": result.settings_target,
                "fix_action": result.fix_action,
                "evidence_refs": list(result.evidence_refs),
            }
            for result in visible
        ],
    }
    return _redact(payload)


def _overall_state(results: tuple[WorkbenchHealthResult, ...]) -> WorkbenchHealthState:
    states = {result.state for result in results}
    for state in (
        WorkbenchHealthState.BROKEN,
        WorkbenchHealthState.APPROVAL_REQUIRED,
        WorkbenchHealthState.BUSY,
        WorkbenchHealthState.STALE,
        WorkbenchHealthState.DEGRADED,
    ):
        if state in states:
            return state
    return WorkbenchHealthState.CONFIGURED


def _state_counts(results: tuple[WorkbenchHealthResult, ...]) -> dict[WorkbenchHealthState, int]:
    return {state: sum(1 for result in results if result.state is state) for state in WorkbenchHealthState}


def _fallback_config() -> WorkbenchStatusConfig:
    return WorkbenchStatusConfig(
        schema_version=SCHEMA_VERSION,
        required_domains=REQUIRED_DOMAINS,
        settings_targets={domain: f"settings.{domain.value}" for domain in REQUIRED_DOMAINS},
        fix_actions={domain: f"inspect-{domain.value}" for domain in REQUIRED_DOMAINS},
    )


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        redacted = _SECRET_PATTERN.sub("[redacted]", value)
        return _SECRET_VALUE_PATTERN.sub("[redacted]", redacted)
    if isinstance(value, Mapping):
        return {
            str(key): "[redacted]" if _SECRET_KEY_PATTERN.search(str(key)) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _iso(value: datetime | None) -> str:
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "REQUIRED_DOMAINS",
    "WorkbenchStatusConfigError",
    "build_assistant_status_context",
    "build_workbench_status_snapshot",
    "load_workbench_status_config",
]
