"""SSRF hostname guard helpers."""

from __future__ import annotations

import ipaddress
import logging

logger = logging.getLogger(__name__)

_METADATA_HOSTS = {
    "169.254.169.254",
    "host.docker.internal",
    "metadata.google.internal",
    "metadata.internal",
}


def check_hostname(hostname: str) -> bool:
    """Return whether a hostname is allowed for outbound fetches.

    Args:
        hostname: Hostname or IP literal to classify.

    Returns:
        False for loopback, private, link-local, and metadata hosts; True otherwise.
    """
    lowered = hostname.strip().lower().strip("[]")
    if lowered in _METADATA_HOSTS or lowered.endswith(".metadata.internal"):
        return False
    ip = _parse_ip_address(_normalize_hostname_to_ip(lowered))
    if ip is None:
        return lowered != "localhost"
    return not (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved)


def _normalize_hostname_to_ip(hostname: str) -> str:
    if hostname.isdigit():
        try:
            value = int(hostname)
            if 0 <= value <= 0xFFFFFFFF:
                return str(ipaddress.ip_address(value))
        except ValueError as exc:
            logger.debug("Could not normalize decimal hostname %r: %s", hostname, exc)
            return hostname
    if hostname.startswith("0x"):
        try:
            return str(ipaddress.ip_address(int(hostname, 16)))
        except ValueError as exc:
            logger.debug("Could not normalize hexadecimal hostname %r: %s", hostname, exc)
            return hostname
    parts = hostname.split(".")
    if len(parts) == 4 and any(part.startswith("0") and len(part) > 1 and part.isdigit() for part in parts):
        try:
            return ".".join(str(int(part, 8)) for part in parts)
        except ValueError as exc:
            logger.debug("Could not normalize octal hostname %r: %s", hostname, exc)
            return hostname
    return hostname


def _parse_ip_address(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse an IP literal, returning None for DNS names."""
    parsed = None
    try:
        parsed = ipaddress.ip_address(hostname)
    except ValueError as exc:
        logger.debug("Hostname %r is not an IP literal: %s", hostname, exc)
        parsed = None
    return parsed
