"""CLI message catalog and lookup helpers.

This is intentionally small: it centralizes owned CLI strings without claiming
repo-wide localization coverage.
"""

from __future__ import annotations

import logging
from string import Formatter
from typing import Any

LOGGER = logging.getLogger(__name__)


class CliTextError(KeyError):
    """Raised when required CLI copy is missing or malformed."""


_DEFAULT_CLI_MESSAGES: dict[str, str] = {
    "health.running": "[AM Workbench] Running health checks...",
    "health.local_ok": "Local inference",
    "health.local_fail": "Local inference",
    "health.adapter_manager": "adapter manager",
    "health.adapter_manager_fail": "Adapter manager",
    "health.hint": "Hint: {hint}",
    "health.reason": "Reason: {reason}",
    "health.unknown": "unknown",
    "backends.header": "provider status cache_durability",
    "backends.unknown": "unknown",
    "init.recovery_loaded": "Recovered previous init wizard state",
}


def cli_text(key: str, default: str | None = None, **values: Any) -> str:
    """Return a CLI message for ``key``.

    Missing keys fall back to ``default`` or the key itself. Unknown format
    fields are ignored by returning the unformatted template.

    Args:
        key: Message key to look up.
        default: Optional fallback text when ``key`` is not registered.
        **values: Optional format values for registered message templates.

    Returns:
        Resolved CLI message text.
    """
    template = _DEFAULT_CLI_MESSAGES.get(key, default if default is not None else key)
    field_names = {name for _, name, _, _ in Formatter().parse(template) if name}
    if not field_names:
        return template
    try:
        return template.format(**{name: values[name] for name in field_names if name in values})
    except (KeyError, ValueError) as exc:
        LOGGER.warning("cli_text_format_failed", extra={"key": key, "error": str(exc)})
        return template


def require_cli_text(key: str, **values: Any) -> str:
    """Return registered CLI copy, failing closed when required text is absent.

    Args:
        key: Registered CLI message key to resolve.
        **values: Format values required by the registered message template.

    Returns:
        Fully formatted CLI message text.

    Raises:
        CliTextError: If ``key`` is not registered, required format values are
            missing, or the stored template is malformed.
    """
    if key not in _DEFAULT_CLI_MESSAGES:
        raise CliTextError(f"required CLI text is not registered: {key}")
    template = _DEFAULT_CLI_MESSAGES[key]
    field_names = {name for _, name, _, _ in Formatter().parse(template) if name}
    missing = sorted(name for name in field_names if name not in values)
    if missing:
        raise CliTextError(f"required CLI text values missing for {key}: {', '.join(missing)}")
    try:
        return template.format(**{name: values[name] for name in field_names})
    except ValueError as exc:
        raise CliTextError(f"required CLI text is malformed for {key}: {exc}") from exc


__all__ = ["CliTextError", "cli_text", "require_cli_text"]
