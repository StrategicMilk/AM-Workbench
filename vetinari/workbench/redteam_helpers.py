"""Private red-team adapter helpers."""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PRIVATE_HTTP_HOSTNAMES = frozenset({"localhost"})


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """Represent ProviderResult state used by Vetinari runtime code."""

    text: str
    timeout: bool = False


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def sanitize_project_id(project_id: str, *, error_cls: type[Exception]) -> str:
    """Sanitize project id for Vetinari callers.

    Args:
        project_id: Project identifier that scopes the operation.
        error_cls: Error cls value consumed by sanitize_project_id().

    Returns:
        Value produced for the caller.

    Raises:
        error_cls: Propagated when validation, persistence, or execution fails.
    """
    if not project_id or not _PROJECT_ID_RE.fullmatch(project_id):
        raise error_cls("project_id contains forbidden characters")
    return project_id


def sanitize_redteam_path(p: Path, *, allowed_root: Path, error_cls: type[Exception]) -> Path:
    """Sanitize redteam path for Vetinari callers.

    Args:
        p: P value consumed by sanitize_redteam_path().
        allowed_root: Allowed root value consumed by sanitize_redteam_path().
        error_cls: Error cls value consumed by sanitize_redteam_path().

    Returns:
        Value produced for the caller.

    Raises:
        error_cls: Propagated when validation, persistence, or execution fails.
    """
    if ".." in p.parts:
        raise error_cls("path contains forbidden traversal segment", path=str(p))
    root = allowed_root.resolve()
    resolved = p.resolve()
    if not resolved.is_relative_to(root):
        raise error_cls("path escapes allowed root", path=str(p))
    return resolved


def sanitize_artifact_path(path_value: str, *, repo_root: Path, error_cls: type[Exception]) -> str:
    """Sanitize artifact path for Vetinari callers.

    Args:
        path_value: Filesystem path read or written by the operation.
        repo_root: Repo root value consumed by sanitize_artifact_path().
        error_cls: Error cls value consumed by sanitize_artifact_path().

    Returns:
        Value produced for the caller.

    Raises:
        error_cls: Propagated when validation, persistence, or execution fails.
    """
    artifact_path = Path(path_value)
    if artifact_path.is_absolute() or ".." in artifact_path.parts:
        raise error_cls("artifact_path contains traversal or absolute path", path=path_value)
    resolved = (repo_root / artifact_path).resolve()
    if not resolved.is_relative_to(repo_root.resolve()):
        raise error_cls("artifact_path escapes repository root", path=path_value)
    return path_value


def clip(text: str, limit: int = 180) -> str:
    """Clamp clip for Vetinari callers.

    Args:
        text: Text value consumed by clip().
        limit: Maximum number of items the operation may return.

    Returns:
        Value produced for the caller.
    """
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def is_private_http_host(hostname: str | None, *, logger: logging.Logger) -> bool:
    """Support is private http host behavior for Vetinari callers.

    Args:
        hostname: Name used to identify the target object.
        logger: Logger used for diagnostic output.

    Returns:
        Value produced for the caller.
    """
    if not hostname:
        return True
    host = hostname.strip("[]").lower()
    if host in _PRIVATE_HTTP_HOSTNAMES:
        return True
    if host.endswith(".example"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            return any(_is_private_address(ipaddress.ip_address(info[4][0])) for info in socket.getaddrinfo(host, None))
        except OSError:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            dns_failed = True
        else:
            dns_failed = False
        if dns_failed:
            return True
    return _is_private_address(address)


def _is_private_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or str(address) == "169.254.169.254"
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "ProviderResult",
    "clip",
    "is_private_http_host",
    "repo_root",
    "sanitize_artifact_path",
    "sanitize_project_id",
    "sanitize_redteam_path",
    "utc_now",
]
