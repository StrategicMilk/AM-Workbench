"""Error message humanization — maps technical exceptions to user-friendly messages.

Loads mappings from ``config/error_messages.yaml`` and provides ``humanize_error()``
to translate raw Python exceptions into messages safe for end users.

This is a support module for the web layer: Intake -> Planning -> Execution ->
Quality Gate -> **Error Reporting** -> Assembly.
"""

from __future__ import annotations

import logging
from typing import Any

import yaml

from vetinari.boundary_guards import account_evidence_drop
from vetinari.config_paths import resolve_config_path

logger = logging.getLogger(__name__)


# Module-level cache — loaded once at first use, never reloaded.
# Who writes: _load_config() on first call.
# Who reads: humanize_error(), humanize_error_message().
# Lock: not needed — assignment is atomic in CPython, worst case two threads both load
# and one result is discarded (idempotent, stateless read).
_config: dict[str, Any] | None = None


def _load_config() -> dict[str, Any]:
    """Load error message mapping from config, with module-level caching.

    Returns:
        Parsed YAML config dict with exception_types, message_patterns, and default.
    """
    global _config
    if _config is not None:
        return _config
    config_path = resolve_config_path("error_messages.yaml")
    try:
        with open(config_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        if not isinstance(raw, dict):
            logger.warning(
                "Error message config at %s has unexpected root type %s — using built-in defaults",
                config_path,
                type(raw).__name__,
            )
            _config = {}
        else:
            _config = raw
    except FileNotFoundError as exc:
        logger.error("config load failed: %s", exc)
        account_evidence_drop(
            logger=logger,
            evidence_ref=str(config_path),
            reason="error_message_config_load_failure",
        )
        logger.warning(
            "Error message config not found at %s — using built-in defaults",
            config_path,
        )
        _config = {}
    except (PermissionError, OSError, yaml.YAMLError) as exc:
        logger.error("config load failed: %s", exc)
        account_evidence_drop(
            logger=logger,
            evidence_ref=str(config_path),
            reason="error_message_config_load_failure",
        )
        logger.exception("Failed to load error message config — using built-in defaults")
        _config = {}
    return _config


def humanize_error(exc: BaseException) -> str:
    """Translate a Python exception into a user-friendly error message.

    Checks exception class name first, then scans error message for known
    patterns. Falls back to a generic message if no match.

    Args:
        exc: The exception to humanize.

    Returns:
        A user-friendly error message string (never contains raw traceback).
    """
    config = _load_config()
    exc_name = type(exc).__name__
    exc_msg = str(exc).lower()

    # 1. Match by exception class name
    exception_types = config.get("exception_types", {})
    if exc_name in exception_types:
        return exception_types[exc_name]

    # Also check fully qualified name (e.g., subprocess.TimeoutExpired)
    fully_qualified_name = f"{type(exc).__module__}.{exc_name}"
    if fully_qualified_name in exception_types:
        return exception_types[fully_qualified_name]

    # 2. Match by message pattern (case-insensitive substring match)
    message_patterns = config.get("message_patterns", {})
    for pattern, friendly_msg in message_patterns.items():
        if pattern.lower() in exc_msg:
            return friendly_msg

    # 3. Special handling for KeyError — name the missing field
    if isinstance(exc, KeyError) and exc.args:
        field_name = exc.args[0]
        return f"Missing required field: '{field_name}'. Check your input data."

    # 4. Default fallback
    return config.get("default", "An unexpected error occurred. Check server logs for details.")


def humanize_error_message(error_message: str) -> str:
    """Translate a raw error message string into a user-friendly message.

    Unlike ``humanize_error()``, this works with string messages rather than
    exception objects. Useful when only the error string is available.

    Args:
        error_message: The raw error message string.

    Returns:
        A user-friendly error message string.
    """
    config = _load_config()
    msg_lower = error_message.lower()

    message_patterns = config.get("message_patterns", {})
    for pattern, friendly_msg in message_patterns.items():
        if pattern.lower() in msg_lower:
            return friendly_msg

    return config.get("default", "An unexpected error occurred. Check server logs for details.")
