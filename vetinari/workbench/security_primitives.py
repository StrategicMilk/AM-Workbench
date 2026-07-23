"""Shared, fail-closed security primitives for all Workbench boundary enforcement."""

from __future__ import annotations

import ipaddress
import re
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from vetinari.workbench.session_kernel import SessionKernelProjectIdRejected

WINDOWS_RESERVED_NAMES: frozenset[str] = frozenset(
    {"CON", "PRN", "AUX", "NUL"} | {f"COM{index}" for index in range(1, 10)} | {f"LPT{index}" for index in range(1, 10)}
)
SECRET_KEY_NAMES: frozenset[str] = frozenset({
    "password",
    "secret",
    "token",
    "api_key",
    "private",
    "credential",
    "auth",
    "bearer",
})

_CANONICAL_ID_RE: re.Pattern[str] = re.compile(r"[A-Za-z0-9_.-]{1,96}")
_TRAVERSAL_MARKERS: tuple[str, ...] = ("/", "\\", "..", "\x00", " ", ";")
_SHELL_UNSAFE_CHARS: frozenset[str] = frozenset(("\n", "\r", "\x00", ";", "|", "&", "`", "$", "'", '"', "\\"))


class BoundaryError(ValueError):
    """Raised when untrusted input crosses a workbench boundary."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def assert_within_root(path: Path | str, *, root: Path | str) -> Path:
    """Resolve a caller path and fail closed if it escapes the trusted root.

    Returns:
        Canonical path contained by ``root``.

    Raises:
        BoundaryError: If the resolved path leaves ``root``.
    """
    original = str(path)
    root_path = Path(root).resolve()
    raw_path = Path(path)
    candidate = (root_path / raw_path).resolve() if not raw_path.is_absolute() else raw_path.resolve()
    if not candidate.is_relative_to(root_path):
        raise BoundaryError("path_escapes_root", original)
    return candidate


def assert_no_symlink_escape(path: Path, *, root: Path) -> Path:
    """Resolve symlinks and fail closed if the final target leaves the root."""
    return assert_within_root(path.resolve(), root=root.resolve())


def canonicalize_safe_id(value: str | None, *, field_name: str = "id") -> str:
    """Return a filesystem-safe id shared by workbench boundary consumers.

    Returns:
        Canonical safe identifier.

    Raises:
        SessionKernelProjectIdRejected: If the value is not a safe id.
    """
    if not isinstance(value, str):
        raise SessionKernelProjectIdRejected(value, field_name=field_name)
    if not value or len(value) > 96 or _CANONICAL_ID_RE.fullmatch(value) is None:
        raise SessionKernelProjectIdRejected(value, field_name=field_name)
    if any(marker in value for marker in _TRAVERSAL_MARKERS):
        raise SessionKernelProjectIdRejected(value, field_name=field_name)
    stem = value.split(".", 1)[0].upper()
    if value == "." or stem in WINDOWS_RESERVED_NAMES:
        raise SessionKernelProjectIdRejected(value, field_name=field_name)
    return value


def assert_trusted_url(
    url: str,
    *,
    allowed_schemes: frozenset[str] = frozenset({"https"}),
    allowed_hosts: frozenset[str] | None = None,
) -> str:
    """Validate scheme, optional host allowlist, and all resolved addresses.

    Returns:
        Original URL when it satisfies the trust policy.

    Raises:
        BoundaryError: If scheme, host, DNS, or address class is untrusted.
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname
    if scheme not in allowed_schemes:
        raise BoundaryError("untrusted_scheme", scheme or "<missing>")
    if not hostname:
        raise BoundaryError("host_missing", "<missing>")
    normalized_host = hostname.lower()
    if allowed_hosts is not None and normalized_host not in {host.lower() for host in allowed_hosts}:
        raise BoundaryError("host_not_allowlisted", normalized_host)
    loopback_allowlisted = allowed_hosts is not None and normalized_host in {"localhost", "127.0.0.1", "::1"}
    try:
        addr_infos = socket.getaddrinfo(hostname, parsed.port, type=socket.SOCK_STREAM)
    except (OSError, socket.gaierror):
        raise BoundaryError("dns_resolution_failed", normalized_host) from None
    if not addr_infos:
        raise BoundaryError("dns_resolution_failed", normalized_host)
    for info in addr_infos:
        address = ipaddress.ip_address(info[4][0])
        if not loopback_allowlisted and (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
        ):
            raise BoundaryError("private_host", normalized_host)
    return url


def scrub_payload(payload: dict[str, Any], *, allowlist: frozenset[str] | None = None) -> dict[str, Any]:
    """Return a scrubbed copy of a payload without mutating caller data.

    Returns:
        Redacted payload copy.
    """
    if allowlist is not None:
        return {key: value for key, value in payload.items() if key in allowlist}
    return {
        key: value
        for key, value in payload.items()
        if not any(secret in str(key).lower() for secret in SECRET_KEY_NAMES)
    }


def shell_safe_token(value: str) -> str:
    """Return a shell token only when it contains no metacharacters.

    Returns:
        Original token when safe for shell argument use.

    Raises:
        BoundaryError: If the token contains shell metacharacters.
    """
    if any(char in value for char in _SHELL_UNSAFE_CHARS):
        raise BoundaryError("shell_unsafe_token", repr(value))
    return value


__all__ = [
    "SECRET_KEY_NAMES",
    "WINDOWS_RESERVED_NAMES",
    "BoundaryError",
    "assert_no_symlink_escape",
    "assert_trusted_url",
    "assert_within_root",
    "canonicalize_safe_id",
    "scrub_payload",
    "shell_safe_token",
]
