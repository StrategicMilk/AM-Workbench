"""Tauri lifecycle command bridge with browser fallback compatibility."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from vetinari.desktop.contracts import (
    LifecycleCommandOrigin,
    LifecycleCommandRequest,
    LifecycleCommandResult,
)
from vetinari.runtime.app_lifecycle import get_app_lifecycle


@dataclass(frozen=True, slots=True)
class TauriLifecycleEnvelope:
    """Renderer-to-main lifecycle command envelope."""

    action: str
    admin_equivalent: bool = False
    force: bool = False
    mode: str | None = None
    shell_window_visible: bool | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> TauriLifecycleEnvelope:
        return cls(
            action=str(payload.get("action", "")),
            admin_equivalent=False,
            force=bool(payload.get("force")),
            mode=payload.get("mode"),
            shell_window_visible=payload.get("shell_window_visible"),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            "TauriLifecycleEnvelope("
            f"action={self.action!r}, admin_equivalent={self.admin_equivalent!r}, force={self.force!r})"
        )


def execute_tauri_lifecycle_command(payload: dict[str, Any]) -> dict[str, Any]:
    """Execute an allowlisted Tauri lifecycle command through the Python controller.

    Returns:
        JSON-compatible lifecycle command result.
    """
    envelope = TauriLifecycleEnvelope.from_payload(payload)
    result = get_app_lifecycle().execute_lifecycle_command(
        LifecycleCommandRequest(
            action=envelope.action,
            origin=LifecycleCommandOrigin.TAURI,
            admin_equivalent=envelope.admin_equivalent,
            force=envelope.force,
            mode=envelope.mode,
            shell_window_visible=envelope.shell_window_visible,
        )
    )
    return result.to_dict()


def execute_browser_fallback_lifecycle_command(payload: dict[str, Any]) -> LifecycleCommandResult:
    """Execute the transitional browser fallback path for non-Tauri launches.

    Returns:
        Lifecycle command result from the shared controller.
    """
    envelope = TauriLifecycleEnvelope.from_payload(payload)
    return get_app_lifecycle().execute_lifecycle_command(
        LifecycleCommandRequest(
            action=envelope.action,
            origin=LifecycleCommandOrigin.BROWSER_FALLBACK,
            admin_equivalent=False,
            force=envelope.force,
            mode=envelope.mode,
            shell_window_visible=envelope.shell_window_visible,
        )
    )


__all__ = [
    "TauriLifecycleEnvelope",
    "execute_browser_fallback_lifecycle_command",
    "execute_tauri_lifecycle_command",
]
