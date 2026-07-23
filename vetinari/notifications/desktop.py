"""Desktop Notifications — OS-native toast notifications and system tray.

Uses ``desktop-notifier`` (cross-platform: Windows WinRT, macOS Notification
Center, Linux D-Bus) for toast notifications and ``pystray`` for a system
tray icon showing Vetinari status.

Both are optional dependencies — if not installed, this module degrades
gracefully by logging a warning and becoming a no-op channel.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from importlib import import_module
from importlib.util import find_spec
from typing import TYPE_CHECKING, Any

from vetinari.security.redaction import redact_text

logger = logging.getLogger(__name__)


# Optional dependency detection
_HAS_DESKTOP_NOTIFIER = False
_HAS_PYSTRAY = False
_HAS_PILLOW = False
_NativeNotifier: Any = None


def _module_is_available(module_name: str) -> bool:
    """Return True when a desktop optional dependency is discoverable."""
    if module_name in sys.modules:
        return sys.modules[module_name] is not None
    try:
        return find_spec(module_name) is not None
    except (ModuleNotFoundError, ValueError):
        logger.warning("Exception handled by  module is available fallback", exc_info=True)
        return False


if _module_is_available("desktop_notifier"):
    _desktop_notifier_module: Any = import_module("desktop_notifier")
    _NativeNotifier = _desktop_notifier_module.DesktopNotifier

    _HAS_DESKTOP_NOTIFIER = True
else:
    logger.debug("desktop-notifier not available — desktop notifications disabled")

if _module_is_available("pystray"):
    import pystray

    _HAS_PYSTRAY = True
else:
    logger.debug("pystray not available — system tray icon disabled")

if _module_is_available("PIL"):
    from PIL import Image as _PillowImage

    _HAS_PILLOW = True
else:
    logger.debug("Pillow not available — system tray icon disabled")

if TYPE_CHECKING:
    from vetinari.notifications.manager import Notification

# Tray status to icon colour mapping (RGB tuples)
_STATUS_COLOURS: dict[str, tuple[int, int, int]] = {
    "healthy": (34, 197, 94),  # green
    "warning": (234, 179, 8),  # yellow
    "error": (239, 68, 68),  # red
}


class DesktopNotificationChannel:
    """Desktop notification channel using OS-native toast notifications.

    Registers as a channel in NotificationManager. If ``desktop-notifier``
    is not installed, all delivery calls are silently skipped with a one-time
    warning log.

    Side effects in __init__:
      - Creates a ``desktop_notifier.DesktopNotifier`` instance (if available)
    """

    def __init__(self) -> None:
        self._notifier: Any = None
        self._tray: SystemTrayIcon | None = None
        self._available = False
        self._warned = False
        if _HAS_DESKTOP_NOTIFIER:
            try:
                self._notifier = _NativeNotifier(app_name="Vetinari")
                self._available = True
                logger.info("Desktop notifications enabled via desktop-notifier")
            except Exception as exc:
                logger.warning(
                    "desktop-notifier backend unavailable — desktop notifications disabled: %s",
                    exc,
                )
        else:
            logger.info(
                "desktop-notifier not installed — desktop notifications disabled. "
                "Install with: pip install vetinari[notifications]"
            )

    def deliver(self, notifications: list[Notification]) -> None:
        """Deliver notifications as OS-native toasts.

        Args:
            notifications: List of Notification objects to display.
        """
        if self._notifier is None:
            if not self._warned:
                logger.warning("Desktop notification delivery skipped — desktop-notifier not installed")
                self._warned = True
            return

        for notification in notifications:
            try:
                # desktop-notifier's sync API (async version available but
                # we use sync for simplicity in the notification handler)
                self._notifier.send_sync(
                    title=redact_text(str(notification.title)),
                    message=redact_text(str(notification.body)),
                )
            except Exception:
                logger.warning(
                    "Failed to send desktop notification %s — OS notification service may be unavailable",
                    notification.notification_id,
                )

    @property
    def is_available(self) -> bool:
        """Whether the desktop notification backend is available.

        Reads ``_HAS_DESKTOP_NOTIFIER`` from the live module in ``sys.modules``
        rather than the function-bound global so that test isolation that
        restores ``vetinari.notifications.desktop`` between collection and
        execution does not produce dual-module-identity false negatives.
        """
        live_module = sys.modules.get(__name__)
        flag = (
            getattr(live_module, "_HAS_DESKTOP_NOTIFIER", _HAS_DESKTOP_NOTIFIER)
            if live_module
            else _HAS_DESKTOP_NOTIFIER
        )
        return getattr(self, "_available", flag) and self._notifier is not None


class SystemTrayIcon:
    """System tray icon showing Vetinari health status via pystray.

    Displays a coloured icon in the OS system tray to give a persistent
    at-a-glance view of Vetinari's state without requiring the dashboard
    to be open.

    Status states map to icon colours:
      - ``healthy`` — green
      - ``warning`` — yellow
      - ``error`` — red

    Menu items: Open Dashboard, Pause/Resume, Status, Quit.

    Gracefully disabled if either ``pystray`` or ``Pillow`` is not installed.

    Side effects in __init__:
      - Starts a daemon thread that runs the pystray icon event loop.
    """

    def __init__(
        self,
        on_open_dashboard: Any = None,
        on_quit: Any = None,
        on_pause_resume: Any = None,
    ) -> None:
        """Initialise the system tray icon.

        Args:
            on_open_dashboard: Optional callable invoked when the user
                clicks "Open Dashboard". Defaults to a no-op.
            on_quit: Optional callable invoked when the user clicks "Quit".
                Defaults to stopping the tray icon.
            on_pause_resume: Optional callable invoked when the user toggles
                Pause/Resume via the tray menu.  Receives one bool argument:
                ``True`` when pausing, ``False`` when resuming.  Without this
                callback the tray only updates its own local state.
        """
        self._status = "healthy"
        self._is_paused = False
        self._lock = threading.Lock()
        self._icon: Any = None
        self._on_open_dashboard = on_open_dashboard
        self._on_quit = on_quit
        self._on_pause_resume = on_pause_resume
        self._available = _HAS_PYSTRAY and _HAS_PILLOW

        if not self._available:
            logger.info(
                "System tray icon disabled — requires both pystray and Pillow. "
                "Install with: pip install vetinari[notifications]"
            )
            return

        self._icon = self._build_icon()

    @staticmethod
    def _make_icon_image(status: str) -> Any:
        """Create a 64x64 coloured PIL image for the given status.

        Args:
            status: One of ``healthy``, ``warning``, or ``error``.

        Returns:
            A PIL Image, or None if Pillow is unavailable.
        """
        if not _HAS_PILLOW:
            return None
        colour = _STATUS_COLOURS.get(status, _STATUS_COLOURS["healthy"])
        try:
            return _PillowImage.new("RGB", (64, 64), colour)
        except Exception:
            logger.warning("Could not create tray icon image for status=%s — using default colour", status)
            return _PillowImage.new("RGB", (64, 64), _STATUS_COLOURS["healthy"])

    def _build_icon(self) -> Any:
        """Assemble the pystray Icon with menu items.

        Returns:
            A configured pystray.Icon instance.
        """
        image = self._make_icon_image(self._status)

        def _open_dashboard(icon: Any, item: Any) -> None:
            if self._on_open_dashboard:
                self._on_open_dashboard()
            else:
                logger.info("Open Dashboard requested via system tray (no handler configured)")

        def _toggle_pause(icon: Any, item: Any) -> None:
            with self._lock:
                self._is_paused = not self._is_paused
                now_paused = self._is_paused
            state = "paused" if now_paused else "resumed"
            logger.info("Vetinari %s via system tray", state)
            if self._on_pause_resume is not None:
                try:
                    self._on_pause_resume(now_paused)
                except Exception as exc:
                    logger.warning(
                        "SystemTrayIcon: pause/resume callback failed — tray state updated "
                        "but scheduler was not notified: %s",
                        exc,
                    )

        def _show_status(icon: Any, item: Any) -> None:
            with self._lock:
                status = self._status
                paused = self._is_paused
            logger.info("System tray status check: status=%s paused=%s", status, paused)

        def _quit(icon: Any, item: Any) -> None:
            if self._on_quit:
                self._on_quit()
            else:
                icon.stop()

        menu = pystray.Menu(
            pystray.MenuItem("Open Dashboard", _open_dashboard),
            pystray.MenuItem(
                "Pause/Resume",
                _toggle_pause,
            ),
            pystray.MenuItem("Status", _show_status),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", _quit),
        )

        return pystray.Icon(
            name="Vetinari",
            icon=image,
            title="Vetinari — AI Development Assistant",
            menu=menu,
        )

    def start(self) -> None:
        """Start the tray icon in a background daemon thread.

        Safe to call multiple times — subsequent calls are ignored if the
        icon is already running.  The daemon thread will stop automatically
        when the main process exits.
        """
        if not self._available or self._icon is None:
            return

        thread = threading.Thread(
            target=self._icon.run,
            name="vetinari-system-tray",
            daemon=True,
        )
        thread.start()
        logger.info("System tray icon started (status=%s)", self._status)

    def update_status(self, status: str) -> None:
        """Update the tray icon colour to reflect a new system status.

        Args:
            status: One of ``healthy``, ``warning``, or ``error``.
                Unknown values fall back to ``healthy``.
        """
        if not self._available or self._icon is None:
            return

        with self._lock:
            self._status = status

        image = self._make_icon_image(status)
        if image is not None:
            try:
                self._icon.icon = image
            except Exception:
                logger.warning("Could not update tray icon for status=%s — icon update failed", status)

    def stop(self) -> None:
        """Stop the tray icon and remove it from the system tray.

        Safe to call even if the icon is not running.
        """
        if self._icon is None:
            return
        try:
            self._icon.stop()
        except Exception:
            logger.warning("Could not stop system tray icon — may already be stopped")

    @property
    def is_available(self) -> bool:
        """Whether the system tray backend is available."""
        return self._available


def _should_start_system_tray() -> bool:
    """Return whether the current process should start a GUI tray thread."""
    if os.environ.get("VETINARI_DISABLE_SYSTEM_TRAY") == "1":
        return False
    return "PYTEST_CURRENT_TEST" not in os.environ


def create_desktop_channel() -> DesktopNotificationChannel | None:
    """Create and register the desktop notification channel and tray icon.

    Starts the system tray icon (if pystray + Pillow are available) alongside
    the desktop notification channel. Both are optional — missing dependencies
    produce a one-time log warning, not an error.

    The tray icon wires its Pause/Resume menu item into the TrainingScheduler
    via an ``on_pause_resume`` callback so that tray interactions actually
    affect the running scheduler, not just local icon state.

    Returns:
        The DesktopNotificationChannel instance, or None if desktop-notifier
        is not installed.  The tray icon may still be running even when None
        is returned — it is stored on the channel when available, or on the
        module-level ``_tray`` reference when the channel is unavailable.
    """

    def _scheduler_pause_resume(pausing: bool) -> None:
        """Forward tray pause/resume to the TrainingScheduler singleton."""
        try:
            from vetinari.training.scheduler import get_training_scheduler

            scheduler = get_training_scheduler()
            if pausing:
                scheduler.pause_for_user_request()
            else:
                scheduler.resume_after_user_request()
        except Exception as exc:
            logger.warning(
                "Tray pause/resume could not reach TrainingScheduler — training state unchanged: %s",
                exc,
            )

    channel = DesktopNotificationChannel()
    tray: SystemTrayIcon | None = None

    if _should_start_system_tray():
        # Start tray icon regardless of desktop-notifier availability.
        # Pass the scheduler callback so Pause/Resume menu items are functional.
        tray = SystemTrayIcon(on_pause_resume=_scheduler_pause_resume)
        tray.start()
    else:
        logger.info("System tray startup skipped in current environment")

    if not channel.is_available:
        # Tray is running but desktop-notifier is absent — keep tray alive via
        # a module-level reference so GC does not stop the daemon thread.
        if tray is not None:
            global _fallback_tray
            _fallback_tray = tray
        return None

    # Store the tray on the channel so callers can update its status icon.
    if tray is not None:
        channel._tray = tray

    try:
        from vetinari.notifications.manager import get_notification_manager

        get_notification_manager().register_channel("desktop", channel.deliver)
    except Exception:
        logger.warning("Failed to register desktop notification channel with manager")

    return channel


# Module-level reference keeping the tray alive when desktop-notifier is absent.
# Written only by create_desktop_channel(); read by nothing (GC anchor only).
_fallback_tray: SystemTrayIcon | None = None
