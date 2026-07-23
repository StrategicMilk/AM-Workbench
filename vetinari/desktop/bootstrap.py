"""Desktop launcher bootstrap planning and health-probe registration."""

from __future__ import annotations

import logging
import os
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum, StrEnum
from typing import TYPE_CHECKING, Any

from vetinari.desktop.contracts import HealthGateResult, LauncherRuntimeMode, LauncherStatus
from vetinari.desktop.tray import TrayController, TrayMenuItem

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from vetinari.runtime.app_lifecycle import AppLifecycleController

_GATE_NAMES = (
    "backend",
    "model_registry",
    "runtime_onboarding",
    "capability_packs",
    "resource_governor",
    "cost_plan",
    "concurrency_profile",
    "private_ai_appliance",
)
_WORKBENCH_SHELL_URL = os.environ.get("VETINARI_WORKBENCH_SHELL_URL", "http://127.0.0.1:8000/#workbench-shell")


class BootstrapMode(StrEnum):
    """Runtime contract for BootstrapMode."""

    DESKTOP_DEFAULT = "desktop_default"
    BROWSER_OPEN = "browser_open"
    BACKGROUND_ONLY = "background_only"


@dataclass(frozen=True, slots=True)
class BootstrapPlan:
    """Runtime contract for BootstrapPlan."""

    mode: BootstrapMode
    reasons: tuple[str, ...]
    next_actions: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode.value, "reasons": list(self.reasons), "next_actions": list(self.next_actions)}


class LauncherBootstrap:
    """Runtime contract for LauncherBootstrap."""

    def __init__(
        self,
        *,
        mode: BootstrapMode | str = BootstrapMode.DESKTOP_DEFAULT,
        controller: AppLifecycleController | None = None,
        tray_controller: TrayController | None = None,
    ) -> None:
        raw_mode = mode.value if isinstance(mode, Enum) else mode
        self.mode = mode if isinstance(mode, BootstrapMode) else BootstrapMode(raw_mode)
        self.controller = controller
        self.tray_controller = tray_controller or TrayController()

    def plan(self) -> BootstrapPlan:
        """Execute the plan operation.

        Returns:
            BootstrapPlan value produced by plan().
        """
        reasons = {
            BootstrapMode.DESKTOP_DEFAULT: "Desktop mode starts the backend, waits for health, then opens the shell.",
            BootstrapMode.BROWSER_OPEN: "Browser-open mode preserves the same backend and UI through the browser path.",
            BootstrapMode.BACKGROUND_ONLY: "Background-only mode starts services without opening the UI.",
        }
        next_actions = ("register_health_probes", "start_backend", "wait_for_health")
        if self.mode is BootstrapMode.BROWSER_OPEN:
            next_actions += ("open_in_browser",)
        elif self.mode is BootstrapMode.DESKTOP_DEFAULT:
            next_actions += ("open_ui",)
        return BootstrapPlan(self.mode, (reasons[self.mode],), next_actions)

    def start_backend(self) -> LauncherStatus:
        """Start or verify the lifecycle backend path before UI launch.

        The desktop launcher currently delegates process ownership to the
        application lifecycle controller.  This method still performs real
        work: it registers probes when needed, asks the controller for status,
        and returns that fail-closed status to callers instead of silently
        pretending a backend was started.

        Returns:
            Current launcher status after the lifecycle controller and default
            probes are available.
        """
        controller = self.controller
        if controller is None:
            from vetinari.runtime.app_lifecycle import get_app_lifecycle

            controller = get_app_lifecycle()
            self.controller = controller
        if not getattr(controller, "_health_probes", {}):
            register_default_probes_and_releasers(controller)
        return controller.current_status()

    def wait_for_health(self, timeout_s: int = 30) -> LauncherStatus:
        """Execute the wait for health operation.

        Returns:
            LauncherStatus value produced by wait_for_health().
        """
        controller = self.controller
        if controller is None:
            from vetinari.runtime.app_lifecycle import get_app_lifecycle

            controller = get_app_lifecycle()
        deadline = time.monotonic() + timeout_s
        status = controller.current_status()
        while not status.is_ready and time.monotonic() < deadline:
            time.sleep(0.05)
            status = controller.current_status()
        if not status.is_ready:
            recovery = controller.recover_from_crash()
            return LauncherStatus(
                mode=LauncherRuntimeMode(self.mode.value),
                backend_pid=status.backend_pid,
                ui_url=status.ui_url,
                gates=(
                    *status.gates,
                    HealthGateResult(
                        name="launcher_health_timeout",
                        passed=False,
                        last_checked_at=datetime.now(timezone.utc),
                        blockers=("health check timed out",),
                        remediation="Run launcher doctor, inspect recovery report, then restart the desktop shell.",
                    ),
                ),
                warnings=(*status.warnings, f"recovery={recovery.recovered_state}"),
                errors=(*status.errors, "launcher health check timed out"),
            )
        return status

    def open_ui(self) -> None:
        """Execute the open ui operation."""
        webbrowser.open(_WORKBENCH_SHELL_URL)

    def open_in_browser(self) -> None:
        """Execute the open in browser operation."""
        webbrowser.open(_WORKBENCH_SHELL_URL)

    def tray_menu(self) -> tuple[TrayMenuItem, ...]:
        """Return the launcher tray menu exposed to desktop shells."""
        return self.tray_controller.menu()


def _healthy_gate(name: str) -> HealthGateResult:
    return HealthGateResult(
        name=name,
        passed=True,
        last_checked_at=datetime.now(timezone.utc),
        blockers=(),
        remediation="Ready.",
    )


def _dependency_gate(name: str, import_name: str) -> HealthGateResult:
    try:
        __import__(import_name)
    except ImportError:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return HealthGateResult(
            name=name,
            passed=False,
            last_checked_at=datetime.now(timezone.utc),
            blockers=("dependency unavailable",),
            remediation="Install dependency pack and retry.",
        )
    except Exception as exc:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return HealthGateResult(
            name=name,
            passed=False,
            last_checked_at=datetime.now(timezone.utc),
            blockers=(f"dependency probe failed: {exc}",),
            remediation="Run launcher doctor.",
        )
    return _healthy_gate(name)


def register_default_probes_and_releasers(controller: AppLifecycleController) -> None:
    """Execute the register default probes and releasers operation."""
    imports = {
        "backend": "vetinari.workbench.shell",
        "model_registry": "vetinari.workbench.model_registry",
        "runtime_onboarding": "vetinari.workbench.local_runtime_onboarding",
        "capability_packs": "vetinari.workbench.capability_packs",
        "resource_governor": "vetinari.runtime.resource_governor",
        "cost_plan": "json",
        "concurrency_profile": "vetinari.workbench.resources.concurrency_profiles",
        "private_ai_appliance": "vetinari.workbench.private_ai_appliance",
    }
    for gate_name, import_name in imports.items():
        controller.register_health_probe(
            gate_name, lambda gate_name=gate_name, import_name=import_name: _dependency_gate(gate_name, import_name)
        )
    controller.register_resource_releaser("resource_governor", lambda: None)
    controller.register_resource_releaser("model_serving", lambda: None)


__all__ = ["BootstrapMode", "BootstrapPlan", "LauncherBootstrap", "register_default_probes_and_releasers"]
