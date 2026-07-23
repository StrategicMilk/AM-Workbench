"""Transport-neutral launcher tray menu contract."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TrayActionId(StrEnum):
    """Runtime contract for TrayActionId."""

    OPEN_UI = "open_ui"
    OPEN_IN_BROWSER = "open_in_browser"
    KEEP_IN_BACKGROUND = "keep_in_background"
    RESTART = "restart"
    QUIT_COMPLETELY = "quit_completely"
    RUN_DOCTOR = "run_doctor"
    SUPPORT_BUNDLE = "support_bundle"


@dataclass(frozen=True, slots=True)
class TrayMenuItem:
    """Runtime contract for TrayMenuItem."""

    id: TrayActionId
    label: str
    enabled: bool = True
    kind: str = "action"

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"TrayMenuItem(id={self.id!r}, label={self.label!r}, enabled={self.enabled!r})"


class TrayController:
    """Runtime contract for TrayController."""

    def menu(self) -> tuple[TrayMenuItem, ...]:
        return (
            TrayMenuItem(TrayActionId.OPEN_UI, "Open AM Workbench"),
            TrayMenuItem(TrayActionId.OPEN_IN_BROWSER, "Open in Browser"),
            TrayMenuItem(TrayActionId.KEEP_IN_BACKGROUND, "Keep in Background"),
            TrayMenuItem(TrayActionId.RESTART, "Restart"),
            TrayMenuItem(TrayActionId.QUIT_COMPLETELY, "Quit Completely"),
            TrayMenuItem(TrayActionId.RUN_DOCTOR, "Run Doctor"),
            TrayMenuItem(TrayActionId.SUPPORT_BUNDLE, "Support Bundle"),
        )


__all__ = ["TrayActionId", "TrayController", "TrayMenuItem"]
