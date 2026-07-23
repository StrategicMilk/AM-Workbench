"""Fail-closed Channel Hub config loading and resolution."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from vetinari.utils.lazy_config import read_yaml_mapping

from .contracts import (
    SCHEMA_VERSION,
    ChannelBlockedReason,
    ChannelDefinition,
    ChannelHealthState,
    ChannelHubConfig,
    ChannelLifecycleState,
    ChannelResolution,
)

logger = logging.getLogger(__name__)


DEFAULT_CHANNEL_CONFIG_PATH = Path("config") / "workbench" / "channels.yaml"


def load_channel_hub_config(config_path: str | Path | None = None) -> ChannelHubConfig:
    """Execute the load channel hub config operation.

    Returns:
        Resolved channel hub config value.
    """
    path = Path(config_path) if config_path is not None else DEFAULT_CHANNEL_CONFIG_PATH
    try:
        raw = read_yaml_mapping(path)
    except FileNotFoundError:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return ChannelHubConfig(
            schema_version=SCHEMA_VERSION, channels=(), source=str(path), config_error="config_missing"
        )
    except OSError as exc:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return ChannelHubConfig(
            schema_version=SCHEMA_VERSION,
            channels=(),
            source=str(path),
            config_error=f"config_unreadable:{type(exc).__name__}",
        )
    except (TypeError, yaml.YAMLError) as exc:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return ChannelHubConfig(
            schema_version=SCHEMA_VERSION,
            channels=(),
            source=str(path),
            config_error=f"config_unreadable:{type(exc).__name__}",
        )
    try:
        channels = tuple(_definition_from_raw(item) for item in raw.get("channels", ()))
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return ChannelHubConfig(
            schema_version=SCHEMA_VERSION,
            channels=(),
            source=str(path),
            config_error=f"config_unreadable:{type(exc).__name__}",
        )
    return ChannelHubConfig(
        schema_version=str(raw.get("schema_version", SCHEMA_VERSION)), channels=channels, source=str(path)
    )


def resolve_channel_definition(config: ChannelHubConfig | str | Path | None, channel_id: str) -> ChannelResolution:
    """Execute the resolve channel definition operation.

    Args:
        config: Config value consumed by resolve_channel_definition().
        channel_id: Channel id value consumed by resolve_channel_definition().

    Returns:
        Resolved channel definition value.
    """
    hub_config = load_channel_hub_config(config) if config is None or isinstance(config, (str, Path)) else config
    clean_id = str(channel_id).strip()
    if hub_config.config_error:
        reason = (
            ChannelBlockedReason.CONFIG_MISSING
            if hub_config.config_error == "config_missing"
            else ChannelBlockedReason.CONFIG_UNREADABLE
        )
        return ChannelResolution(clean_id, None, False, reason, hub_config.config_error)
    for definition in hub_config.channels:
        if definition.channel_id == clean_id:
            return _resolve_definition(definition)
    return ChannelResolution(clean_id, None, False, ChannelBlockedReason.CHANNEL_UNKNOWN, "channel is not registered")


def _resolve_definition(definition: ChannelDefinition) -> ChannelResolution:
    if definition.lifecycle_state is not ChannelLifecycleState.ENABLED:
        return ChannelResolution(
            definition.channel_id,
            definition,
            False,
            ChannelBlockedReason.CHANNEL_DISABLED,
            f"channel lifecycle is {definition.lifecycle_state.value}",
        )
    if definition.health_state in {ChannelHealthState.UNHEALTHY, ChannelHealthState.UNKNOWN}:
        return ChannelResolution(
            definition.channel_id,
            definition,
            False,
            ChannelBlockedReason.CHANNEL_UNHEALTHY,
            f"channel health is {definition.health_state.value}",
        )
    if definition.health_state is ChannelHealthState.DEGRADED:
        return ChannelResolution(
            definition.channel_id,
            definition,
            False,
            ChannelBlockedReason.CHANNEL_DEGRADED,
            "degraded channel requires explicit downstream handling",
        )
    return ChannelResolution(definition.channel_id, definition, True, "", "channel resolved")


def _definition_from_raw(raw: Any) -> ChannelDefinition:
    if not isinstance(raw, dict):
        raise ValueError("channel definition must be a mapping")
    data = dict(raw)
    return ChannelDefinition(
        channel_id=str(data["channel_id"]),
        channel_type=str(data["channel_type"]),
        display_name=str(data["display_name"]),
        lifecycle_state=str(data["lifecycle_state"]),
        health_state=str(data["health_state"]),
        capabilities=tuple(data.get("capabilities", ())),
        default_target=str(data["default_target"]),
        redaction_policy=dict(data.get("redaction_policy", {})),
        approval_policy=str(data.get("approval_policy", "none")),
        command_authorization_policy=str(data.get("command_authorization_policy", "none")),
        health_detail=str(data.get("health_detail", "")),
        metadata=dict(data.get("metadata", {})),
    )
