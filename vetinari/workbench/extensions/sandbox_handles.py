"""Sandbox handle cards and admission defaults for Workbench extensions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True, slots=True)
class SandboxHandle:
    """One active or historical sandbox handle."""

    sandbox_id: str
    run_id: str
    image_profile: str
    write_scope: str
    egress_allowlist: tuple[str, ...] = ()
    network_enabled: bool = False
    teardown_status: str = "active"
    replay_boundary_ref: str = ""

    def __post_init__(self) -> None:
        for field_name in ("sandbox_id", "run_id", "image_profile", "write_scope"):
            _require_text(getattr(self, field_name), field_name)
        if self.network_enabled and not self.egress_allowlist:
            raise ValueError("network requires an explicit egress allowlist")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SandboxHandle(sandbox_id={self.sandbox_id!r}, run_id={self.run_id!r}, image_profile={self.image_profile!r})"


@dataclass(frozen=True, slots=True)
class SandboxAdmission:
    """Decision for a sandbox exec, file, or network operation."""

    admitted: bool
    blockers: tuple[str, ...]
    handle: SandboxHandle


def admit_sandbox_operation(
    handle: SandboxHandle,
    *,
    path: str = "",
    egress_host: str = "",
) -> SandboxAdmission:
    """Fail closed on network and write scope before sandbox execution.

    Returns:
        SandboxAdmission value produced by admit_sandbox_operation().
    """
    blockers: list[str] = []
    if egress_host and (not handle.network_enabled or egress_host not in handle.egress_allowlist):
        blockers.append("network_not_allowlisted")
    if path and not _inside_scope(path, handle.write_scope):
        blockers.append("write_scope_escape")
    if handle.teardown_status != "active":
        blockers.append("sandbox_not_active")
    return SandboxAdmission(not blockers, tuple(blockers), handle)


def _inside_scope(path: str, scope: str) -> bool:
    path_parts = _canonical_parts(path)
    scope_parts = _canonical_parts(scope)
    return path_parts == scope_parts or path_parts[: len(scope_parts)] == scope_parts


def _canonical_parts(value: str) -> tuple[str, ...]:
    normalized = PurePosixPath(value.replace("\\", "/"))
    parts: list[str] = []
    for part in normalized.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts or parts[-1] == "..":
                parts.append(part)
            else:
                parts.pop()
            continue
        parts.append(part)
    return tuple(parts)


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


__all__ = ["SandboxAdmission", "SandboxHandle", "admit_sandbox_operation"]
