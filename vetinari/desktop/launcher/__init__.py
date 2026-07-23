"""AM Workbench desktop launcher entry package."""

from __future__ import annotations

from vetinari.desktop.launcher.tauri_bridge import (
    TauriLifecycleEnvelope,
    execute_browser_fallback_lifecycle_command,
    execute_tauri_lifecycle_command,
)

__all__ = [
    "TauriLifecycleEnvelope",
    "execute_browser_fallback_lifecycle_command",
    "execute_tauri_lifecycle_command",
]
