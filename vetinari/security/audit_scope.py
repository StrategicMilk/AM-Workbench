"""Project audit-scope validation helpers."""

from __future__ import annotations

from typing import Any


class AuditScopeError(ValueError):
    """Raised when an audit write lacks a real project scope."""


def require_project_id(project_id: object, *, field: str = "project_id") -> str:
    """Return a stripped project id, rejecting blank and default scopes.

    Returns:
        Validated project id.

    Raises:
        AuditScopeError: If the project id is missing or generic.
    """
    if not isinstance(project_id, str) or not project_id.strip() or project_id.strip() == "default":
        raise AuditScopeError(f"{field} is required and must be project-specific")
    return project_id.strip()


def scoped_asset_write(
    *,
    asset_id: str,
    kind: str,
    project_id: object,
    path: str | None = None,
    redact_fields: list[str] | None = None,
) -> Any:
    """Validate project scope before forwarding an asset-write audit event.

    Returns:
        Result returned by the spine consumer.
    """
    from vetinari.workbench.spine_consumers import record_asset_written

    return record_asset_written(
        asset_id=asset_id,
        kind=kind,
        project_id=require_project_id(project_id),
        path=path,
        redact_fields=redact_fields,
    )
