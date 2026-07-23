"""Update channel configuration loader."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from vetinari.workbench.update_safety.contracts import UpdateChannel, UpdateSafetyError

SCHEMA_VERSION = "1.0"
DEFAULT_CHANNEL_CONFIG_PATH = Path("config") / "workbench" / "update_channels.yaml"


@dataclass(frozen=True, slots=True)
class UpdateChannelPolicy:
    """Static policy for one update channel."""

    channel: UpdateChannel
    enabled: bool
    manifest_path: str
    require_signature: bool
    allow_auto_install: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel.value,
            "enabled": self.enabled,
            "manifest_path": self.manifest_path,
            "require_signature": self.require_signature,
            "allow_auto_install": self.allow_auto_install,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"UpdateChannelPolicy(channel={self.channel!r}, enabled={self.enabled!r}, manifest_path={self.manifest_path!r})"


@dataclass(frozen=True, slots=True)
class UpdateChannelConfig:
    """Validated update channel config."""

    schema_version: str
    default_channel: UpdateChannel
    channels: dict[UpdateChannel, UpdateChannelPolicy]

    def policy_for(self, channel: UpdateChannel | str) -> UpdateChannelPolicy:
        """Execute the policy for operation.

        Returns:
            UpdateChannelPolicy value produced by policy_for().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        requested = UpdateChannel(str(channel))
        policy = self.channels.get(requested)
        if policy is None or not policy.enabled:
            raise UpdateSafetyError(f"channel_unavailable:{requested.value}")
        if policy.allow_auto_install:
            raise UpdateSafetyError(f"channel_auto_install_forbidden:{requested.value}")
        return policy


def load_update_channel_config(path: str | Path = DEFAULT_CHANNEL_CONFIG_PATH) -> UpdateChannelConfig:
    """Load channel config and fail closed for malformed or unknown channels.

    Returns:
        Resolved update channel config value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise UpdateSafetyError(f"channel_config_unreadable:{type(exc).__name__}") from exc
    except yaml.YAMLError as exc:
        raise UpdateSafetyError(f"channel_config_corrupt:{type(exc).__name__}") from exc
    if not isinstance(raw, dict):
        raise UpdateSafetyError("channel_config_root_invalid")
    if str(raw.get("schema_version")) != SCHEMA_VERSION:
        raise UpdateSafetyError(f"channel_config_schema_mismatch:{SCHEMA_VERSION}")
    channels_raw = raw.get("channels")
    if not isinstance(channels_raw, dict):
        raise UpdateSafetyError("channel_config_channels_invalid")
    channels: dict[UpdateChannel, UpdateChannelPolicy] = {}
    for name, entry in channels_raw.items():
        channel = UpdateChannel(str(name))
        if not isinstance(entry, dict):
            raise UpdateSafetyError(f"channel_policy_invalid:{channel.value}")
        channels[channel] = UpdateChannelPolicy(
            channel=channel,
            enabled=bool(entry.get("enabled", False)),
            manifest_path=str(entry.get("manifest_path", "")).strip(),
            require_signature=bool(entry.get("require_signature", channel is UpdateChannel.STABLE)),
            allow_auto_install=bool(entry.get("allow_auto_install", False)),
        )
        if channels[channel].enabled and not channels[channel].manifest_path:
            raise UpdateSafetyError(f"channel_manifest_missing:{channel.value}")
    default_channel = UpdateChannel(str(raw.get("default_channel", UpdateChannel.STABLE.value)))
    if default_channel not in channels:
        raise UpdateSafetyError(f"default_channel_unknown:{default_channel.value}")
    return UpdateChannelConfig(schema_version=SCHEMA_VERSION, default_channel=default_channel, channels=channels)


__all__ = [
    "DEFAULT_CHANNEL_CONFIG_PATH",
    "UpdateChannelConfig",
    "UpdateChannelPolicy",
    "load_update_channel_config",
]
