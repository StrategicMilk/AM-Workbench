"""Fail-closed outbound URL validation for owned HTTP adapters."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable
from urllib.parse import urlparse


class SSRFGuardError(ValueError):
    """Raised when an outbound URL is not safe to request."""


_BLOCKED_HOSTS = {"localhost", "localhost.localdomain"}


def validate_outbound_url(
    url: str,
    *,
    allowed_hosts: Iterable[str] | None = None,
    resolve_hostname: bool = True,
) -> str:
    """Return ``url`` only when it is safe for outbound HTTP.

    The guard rejects non-HTTP schemes, userinfo, missing hosts, localhost,
    private/reserved/link-local IPs, and hostnames resolving exclusively to
    blocked addresses. ``allowed_hosts`` can narrow the policy further.

    Returns:
        The original URL when it passes outbound safety checks.

    Raises:
        SSRFGuardError: If the URL or resolved host is unsafe.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise SSRFGuardError("outbound URL scheme must be http or https")
    if not parsed.hostname:
        raise SSRFGuardError("outbound URL host is required")
    if parsed.username or parsed.password:
        raise SSRFGuardError("outbound URL userinfo is not allowed")
    hostname = parsed.hostname.rstrip(".").lower()
    if allowed_hosts is not None and hostname not in {host.rstrip(".").lower() for host in allowed_hosts}:
        raise SSRFGuardError("outbound URL host is not allowlisted")
    if hostname in _BLOCKED_HOSTS:
        raise SSRFGuardError("outbound URL host is local")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if resolve_hostname:
            _validate_resolved_addresses(hostname)
    else:
        _validate_ip_address(address)
    return url


def _validate_resolved_addresses(hostname: str) -> None:
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SSRFGuardError("outbound URL host could not be resolved") from exc
    if not infos:
        raise SSRFGuardError("outbound URL host could not be resolved")
    for info in infos:
        _validate_ip_address(ipaddress.ip_address(info[4][0]))


def _validate_ip_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise SSRFGuardError("outbound URL resolves to a non-public address")


__all__ = ["SSRFGuardError", "validate_outbound_url"]
