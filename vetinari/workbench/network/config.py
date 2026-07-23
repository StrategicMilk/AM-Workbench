"""Fail-closed config loader for network transport policy."""

from __future__ import annotations

import fnmatch
import ipaddress
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from vetinari.contracts import ConfigContractViolation, fail_closed_config_load
from vetinari.workbench.network.contracts import NetworkTransportError, NetworkTransportPolicy

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_NETWORK_TRANSPORT_CONFIG = _PROJECT_ROOT / "config" / "workbench" / "network_transport.yaml"
DEFAULT_NETWORK_POLICY_CONFIG = _PROJECT_ROOT / "vetinari" / "config" / "runtime" / "network_policy.yaml"
logger = logging.getLogger(__name__)


class NetworkPolicyViolation(PermissionError):
    """Raised when a URL is denied by the runtime network isolation policy."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def load_network_transport_policy(path: Path | str = DEFAULT_NETWORK_TRANSPORT_CONFIG) -> NetworkTransportPolicy:
    """Load governed network policy from YAML.

    Returns:
        Resolved network transport policy value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    config_path = Path(path)
    if not config_path.exists():
        raise NetworkTransportError("network-config-missing", str(config_path))
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise NetworkTransportError("network-config-unreadable", type(exc).__name__) from exc
    if not isinstance(raw, dict):
        raise NetworkTransportError("network-config-invalid", "root must be a mapping")
    policy = raw.get("policy", raw)
    if not isinstance(policy, dict):
        raise NetworkTransportError("network-policy-invalid", "policy must be a mapping")
    return _policy_from_mapping(policy)


def _policy_from_mapping(raw: dict[str, Any]) -> NetworkTransportPolicy:
    try:
        return NetworkTransportPolicy(
            bandwidth_budget_mbps=float(_required_policy_value(raw, "bandwidth_budget_mbps")),
            max_retry_backoff_seconds=float(_required_policy_value(raw, "max_retry_backoff_seconds")),
            cache_ttl_seconds=int(_required_policy_value(raw, "cache_ttl_seconds")),
            stale_after_seconds=int(_required_policy_value(raw, "stale_after_seconds")),
            preferred_providers=tuple(str(item) for item in raw.get("preferred_providers", ())),
            risky_change_requires_approval=bool(raw.get("risky_change_requires_approval", True)),
            allow_host_network_mutation=bool(raw.get("allow_host_network_mutation")),
        )
    except NetworkTransportError:
        raise
    except (TypeError, ValueError) as exc:
        raise NetworkTransportError("network-policy-invalid", type(exc).__name__) from exc


def _required_policy_value(raw: dict[str, Any], key: str) -> Any:
    try:
        return raw[key]
    except KeyError as exc:
        raise NetworkTransportError("network-policy-missing-key", key) from exc


def validate_network_policy(path: Path | str = DEFAULT_NETWORK_POLICY_CONFIG) -> dict[str, Any]:
    """Load and validate the runtime network isolation policy.

    The policy is intentionally validated here instead of being left as design
    documentation: an empty policy, missing mode table, or unknown default mode
    blocks startup/config admission.

    Returns:
        Parsed policy mapping.

    Raises:
        ConfigContractViolation: When the policy is missing, unreadable, empty,
            or disconnected from a declared default mode.
    """
    payload = fail_closed_config_load(path)
    if not isinstance(payload, dict):
        raise ConfigContractViolation(path=Path(path), reason="Network policy root must be a mapping")
    modes = payload.get("modes")
    if not isinstance(modes, dict) or not modes:
        raise ConfigContractViolation(path=Path(path), reason="Network policy must define at least one mode")
    default_mode = payload.get("default_mode")
    if not isinstance(default_mode, str) or default_mode not in modes:
        raise ConfigContractViolation(path=Path(path), reason="Network policy default_mode must name a defined mode")
    for mode_name, mode in modes.items():
        if not isinstance(mode, dict):
            raise ConfigContractViolation(path=Path(path), reason=f"Network mode {mode_name!r} must be a mapping")
        if "allow_loopback" not in mode:
            raise ConfigContractViolation(
                path=Path(path),
                reason=f"Network mode {mode_name!r} must declare allow_loopback",
            )
        allowed_hosts = mode.get("allowed_hosts")
        if not isinstance(allowed_hosts, list) or any(not isinstance(host, str) for host in allowed_hosts):
            raise ConfigContractViolation(
                path=Path(path),
                reason=f"Network mode {mode_name!r} must declare allowed_hosts as a string list",
            )
    return payload


def active_network_mode(policy: dict[str, Any]) -> str:
    """Return the active network policy mode from env or policy default.

    Returns:
        Name of the selected policy mode.

    Raises:
        NetworkPolicyViolation: If the selected mode is not declared.
    """
    modes = policy.get("modes", {})
    requested = os.environ.get("VETINARI_NETWORK_MODE") or policy.get("default_mode")
    if not isinstance(requested, str) or requested not in modes:
        raise NetworkPolicyViolation("network_mode_invalid", str(requested))
    return requested


def enforce_network_policy_url(
    url: str,
    *,
    policy_path: Path | str = DEFAULT_NETWORK_POLICY_CONFIG,
    mode: str | None = None,
) -> str:
    """Validate one outbound URL against the runtime network isolation policy.

    This is the admission hook network-capable callers use before opening a
    socket. It fails closed on malformed URLs, unknown modes, blocked hosts, and
    disallowed ports. The function intentionally returns the original URL on
    success so callers can compose it inline with existing request setup.

    Returns:
        The original URL when policy admission succeeds.

    Raises:
        NetworkPolicyViolation: If the URL is denied by the active mode.
    """
    policy = validate_network_policy(policy_path)
    selected_mode = mode or active_network_mode(policy)
    modes = policy["modes"]
    if selected_mode not in modes:
        raise NetworkPolicyViolation("network_mode_invalid", selected_mode)
    mode_policy = modes[selected_mode]

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() not in {"http", "https"}:
        raise NetworkPolicyViolation("network_scheme_blocked", parsed.scheme or "<missing>")
    if not host:
        raise NetworkPolicyViolation("network_host_missing", "<missing>")

    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    if selected_mode == "restricted" and port not in {int(value) for value in policy.get("restricted_ports", [])}:
        raise NetworkPolicyViolation("network_port_blocked", str(port))

    if _is_loopback_host(host):
        if mode_policy.get("allow_loopback") is True:
            return url
        raise NetworkPolicyViolation("network_loopback_blocked", host)

    allowed_hosts = [str(item).lower() for item in mode_policy.get("allowed_hosts", [])]
    if "*" in allowed_hosts:
        return url
    if selected_mode == "offline":
        raise NetworkPolicyViolation("network_offline", host)
    if not any(_host_matches(host, pattern) for pattern in allowed_hosts):
        raise NetworkPolicyViolation("network_host_blocked", host)
    return url


def _host_matches(host: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(host, pattern)


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        logger.debug("network policy host is not an IP literal: %s", host)
        return False


__all__ = [
    "DEFAULT_NETWORK_POLICY_CONFIG",
    "DEFAULT_NETWORK_TRANSPORT_CONFIG",
    "NetworkPolicyViolation",
    "active_network_mode",
    "enforce_network_policy_url",
    "load_network_transport_policy",
    "validate_network_policy",
]
