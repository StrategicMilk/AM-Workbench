"""CLI startup helpers — system wiring and initialization for Vetinari.

Responsible for logging setup, config loading, orchestrator construction,
and wiring all optional subsystems together at startup.  Every wiring step
is non-fatal: a missing subsystem logs a warning and startup continues.

This is an internal support module consumed by ``vetinari.cli``.
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
from pathlib import Path

from vetinari.cli_startup_wiring import (
    _SKILL_MODULES as _SKILL_MODULES,
)
from vetinari.cli_startup_wiring import (
    _instantiate_tool_class as _instantiate_tool_class,
)
from vetinari.cli_startup_wiring import (
    _wire_alert_evaluation as _wire_alert_evaluation,
)
from vetinari.cli_startup_wiring import (
    _wire_analytics_to_dashboard as _wire_analytics_to_dashboard,
)
from vetinari.cli_startup_wiring import (
    _wire_autonomy_and_notifications as _wire_autonomy_and_notifications,
)
from vetinari.cli_startup_wiring import (
    _wire_drift_to_orchestration as _wire_drift_to_orchestration,
)
from vetinari.cli_startup_wiring import (
    _wire_durable_recovery as _wire_durable_recovery,
)
from vetinari.cli_startup_wiring import (
    _wire_event_subscribers as _wire_event_subscribers,
)
from vetinari.cli_startup_wiring import (
    _wire_learning_to_dashboard as _wire_learning_to_dashboard,
)
from vetinari.cli_startup_wiring import (
    _wire_security_to_verification as _wire_security_to_verification,
)
from vetinari.cli_startup_wiring import (
    _wire_skills_to_registry as _wire_skills_to_registry,
)
from vetinari.cli_startup_wiring import (
    _wire_sse_event_cleanup as _wire_sse_event_cleanup,
)
from vetinari.cli_startup_wiring import (
    _wire_telemetry_persistence as _wire_telemetry_persistence,
)
from vetinari.cli_startup_wiring import (
    _wire_tracing_instrumentation as _wire_tracing_instrumentation,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def _is_debug_mode() -> bool:
    """Check if VETINARI_DEBUG=1 environment variable is set.

    Returns:
        True if VETINARI_DEBUG is set to 1, true, or yes.
    """
    return os.environ.get("VETINARI_DEBUG", "").strip() in ("1", "true", "yes")


def _setup_logging(verbose: bool = False) -> None:
    """Configure root logging for the Vetinari process.

    When ``verbose`` or ``VETINARI_DEBUG=1`` is set, switches to DEBUG level
    and adds millisecond timestamps and line numbers to log output.

    Args:
        verbose: If True, enable DEBUG logging regardless of environment.
    """
    debug_mode = _is_debug_mode()
    level = logging.DEBUG if (verbose or debug_mode) else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    if verbose or debug_mode:
        # Enhanced format with timing and module location — active when verbose or VETINARI_DEBUG=1
        fmt = "%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s:%(lineno)d: %(message)s"
    logging.basicConfig(
        level=level,
        format=fmt,
        datefmt="%H:%M:%S",
    )
    if debug_mode:
        logging.getLogger("vetinari").setLevel(logging.DEBUG)
        logger.debug("VETINARI_DEBUG mode active — all debug logs promoted")


# ---------------------------------------------------------------------------
# Config and orchestrator construction
# ---------------------------------------------------------------------------


def _load_config(config_path: str) -> dict:
    """Load a YAML manifest config file, falling back to defaults if missing.

    Tries the path as given, then relative to the package root. Returns
    a minimal default config when the file cannot be found.

    Args:
        config_path: Relative or absolute path to the YAML manifest.

    Returns:
        Parsed YAML content as a dict, or a minimal default dict.
    """
    import yaml

    p = Path(config_path)
    if not p.exists():
        # Try relative to package directory
        pkg_root = Path(__file__).resolve().parents[1]
        p = pkg_root / config_path
    if not p.exists():
        logger.warning("Config file not found: %s, using defaults", config_path)
        return {"project_name": "vetinari", "tasks": []}
    with Path(p).open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        logger.warning(
            "Config file %s has unexpected format (expected dict, got %s) — using defaults",
            config_path,
            type(data).__name__,
        )
        return {"project_name": "vetinari", "tasks": []}
    return data


def _build_orchestrator(config_path: str, mode: str = "execution"):
    """Construct an Orchestrator for the given config path and mode.

    Args:
        config_path: Path to the manifest YAML file.
        mode: Execution mode (planning, execution, or sandbox).

    Returns:
        A configured Orchestrator instance.
    """
    from vetinari.orchestrator import Orchestrator

    return Orchestrator(config_path, execution_mode=mode)


# ---------------------------------------------------------------------------
# Startup banner
# ---------------------------------------------------------------------------


def _print_banner(mode: str) -> None:
    """Print the Vetinari startup banner.

    When VETINARI_DEBUG=1 is set, also prints feature status, model
    availability, and system information.

    Args:
        mode: The execution mode to display in the banner.
    """
    print("=" * 60)
    print(" VETINARI AI Orchestration System")
    print(f" Mode: {mode.upper()}")
    if _is_debug_mode():
        import time

        print(" Debug: ENABLED (VETINARI_DEBUG=1)")
        print(f" Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f" Python: {sys.version.split()[0]}")
        # Feature status
        try:
            from vetinari.constants import (
                OPERATOR_MODELS_CACHE_DIR,
                get_user_dir,
            )

            print(f" Models dir: {OPERATOR_MODELS_CACHE_DIR}")
            print(f" User dir: {get_user_dir()}")
        except ImportError:
            logging.getLogger(__name__).debug("startup constants unavailable in minimal install", exc_info=True)
        if importlib.util.find_spec("llama_cpp") is not None:
            print(" llama-cpp-python: available")
        else:
            print(" llama-cpp-python: NOT installed")
    print("=" * 60)


# ---------------------------------------------------------------------------
# Drift check at startup
# ---------------------------------------------------------------------------


def _check_drift_at_startup() -> None:
    """Run contract drift check at startup (non-fatal).

    Queries the drift monitor for any contract or schema changes since the
    last baseline.  Logs warnings but never prevents startup.
    """
    try:
        from vetinari.drift.monitor import get_drift_monitor

        monitor = get_drift_monitor()
        report = monitor.run_full_audit()
        if not report.is_clean:
            for issue in report.issues:
                logger.warning("[Drift] %s", issue)
            print("[AM Workbench] WARNING: Contract drift detected. Run 'vetinari drift-check' for details.")
    except Exception as e:
        logger.warning("Drift check skipped: %s", e)


# ---------------------------------------------------------------------------
# Subsystem wiring
# ---------------------------------------------------------------------------


def _wire_subsystems() -> None:
    """Connect all Vetinari subsystems together at startup.

    Wires:
    1. Graceful shutdown handlers (SIGTERM/SIGINT + atexit)
    2. Learning pipeline -> web dashboard API blueprints
    3. Drift monitor -> orchestration pre-check hook
    4. Analytics -> web dashboard API blueprints
    5. Security scanner -> verification pipeline
    6. Skill Tool subclasses -> ToolRegistry (auto-registration)
    7. Durable execution recovery
    8. EventBus domain subscribers
    9. TelemetryPersistence background flush loop

    All wiring steps are non-fatal: failures are logged as warnings so that a
    missing optional subsystem never prevents Vetinari from starting.
    """
    try:
        from vetinari.shutdown import register_shutdown_handlers

        register_shutdown_handlers()
    except Exception as exc:
        logger.warning("Wiring: shutdown handlers failed: %s", exc)
    _wire_tracing_instrumentation()
    _wire_learning_to_dashboard()
    _wire_drift_to_orchestration()
    _wire_analytics_to_dashboard()
    _wire_security_to_verification()
    _wire_skills_to_registry()
    _wire_durable_recovery()
    _wire_event_subscribers()
    _wire_sse_event_cleanup()
    _wire_telemetry_persistence()
    _wire_alert_evaluation()
    _wire_autonomy_and_notifications()
    logger.info("Startup wiring complete")
