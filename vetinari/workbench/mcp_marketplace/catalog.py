"""Fail-closed catalog loader for Workbench MCP and plugin marketplace metadata."""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
import yaml

from vetinari.constants import PROJECT_ROOT
from vetinari.workbench.extensions.contracts import (
    ExtensionContractError,
    ExtensionManifest,
    ExtensionRiskReason,
    ExtensionRiskStatus,
    ExtensionRiskVerdict,
    evaluate_manifest_risk,
)

logger = logging.getLogger(__name__)


_CATALOG_SCHEMA_VERSION = 1
_CATALOG_PATH = PROJECT_ROOT / "config" / "workbench" / "extension_marketplace.yaml"
_CATALOG_LOCK = threading.Lock()
_CATALOG_CACHE: tuple[ExtensionManifest, ...] | None = None
_OAUTH_TOKEN_TIMEOUT_SECONDS = (5, 30)
_OAUTH_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
_REDACTION_SENTINEL = "*" * 8


class ExtensionMarketplaceError(RuntimeError):
    """Raised when marketplace metadata cannot be trusted."""


@dataclass(frozen=True, slots=True)
class OAuthAuthorizationRequest:
    """PKCE authorization request for an OAuth-backed marketplace install."""

    extension_id: str
    provider: str
    authorization_url: str
    state: str
    code_verifier: str
    code_challenge: str
    scopes: tuple[str, ...]
    redirect_uri: str
    client_id: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize the PKCE authorization request for the admin API.

        Returns:
            JSON-compatible request details that the caller must retain to
            complete the authorization-code callback.
        """
        return {
            "extension_id": self.extension_id,
            "provider": self.provider,
            "authorization_url": self.authorization_url,
            "state": self.state,
            "code_verifier": self.code_verifier,
            "code_challenge": self.code_challenge,
            "scopes": list(self.scopes),
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "code_challenge_method": "S256",
        }

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"extension_id={self.extension_id!r}, "
            f"provider={self.provider!r}, "
            f"authorization_url={self.authorization_url!r}, "
            f"state={self.state!r}"
            ")"
        )


@dataclass(frozen=True, slots=True)
class OAuthTokenExchangeResult:
    """Secret-bearing OAuth token response with redacted public serialization."""

    extension_id: str
    provider: str
    token_type: str
    access_token: str
    refresh_token: str | None
    scopes: tuple[str, ...]
    expires_in_seconds: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize token metadata without exposing bearer or refresh secrets.

        Returns:
            JSON-compatible metadata safe for API responses and logs. Token
            fields are deliberately present but redacted so callers can tell
            whether the provider supplied them without learning the value.
        """
        return {
            "extension_id": self.extension_id,
            "provider": self.provider,
            "token_type": self.token_type,
            "access_token": _REDACTION_SENTINEL,
            "refresh_token": _REDACTION_SENTINEL if self.refresh_token is not None else None,
            "scopes": list(self.scopes),
            "expires_in_seconds": self.expires_in_seconds,
        }

    def authorization_headers(self) -> dict[str, str]:
        """Build an explicit Authorization header for MCP transport clients.

        Returns:
            A header dictionary that intentionally contains the bearer token.
            Callers must pass this directly to transport clients rather than
            serializing it into public API payloads.
        """
        return {"Authorization": f"{self.token_type} {self.access_token}"}

    def __repr__(self) -> str:
        """Return a diagnostic representation that never includes token values."""
        return (
            f"{type(self).__name__}("
            f"extension_id={self.extension_id!r}, "
            f"provider={self.provider!r}, "
            f"token_type={self.token_type!r}, "
            f"scopes={self.scopes!r}, "
            f"expires_in_seconds={self.expires_in_seconds!r}"
            ")"
        )


def _fail_closed_verdict(reason: ExtensionRiskReason, detail: str) -> ExtensionRiskVerdict:
    return ExtensionRiskVerdict(
        status=ExtensionRiskStatus.BLOCKED,
        allowed=False,
        reasons=(reason,),
        disabled_by_default=True,
        manual_selection_required=True,
        details={"error": detail},
    )


def _manifest_from_mapping(raw: Mapping[str, Any]) -> ExtensionManifest:
    try:
        manifest = ExtensionManifest.from_mapping(raw)
    except (ExtensionContractError, ValueError, TypeError) as exc:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        extension_id = str(raw.get("extension_id", "unknown")) if isinstance(raw, Mapping) else "unknown"
        repaired = dict(raw)
        repaired.setdefault("schema_version", "1.0")
        repaired.setdefault("source_kind", "plugin")
        repaired.setdefault("version", "unknown")
        repaired.setdefault("compatibility", "unknown")
        repaired.setdefault("declared_tools", [])
        repaired.setdefault("requested_secrets", [])
        repaired.setdefault("stdio", False)
        repaired.setdefault("network", False)
        repaired.setdefault("filesystem", False)
        repaired.setdefault("dependencies", [])
        repaired.setdefault("dependency_findings", [])
        repaired.setdefault("overlap_categories", [])
        repaired.setdefault("destructive_capabilities", [])
        repaired["authority_owner"] = "workbench"
        repaired["disabled_by_default"] = True
        repaired["marketplace_metadata"] = {
            "source_id": str(raw.get("marketplace_metadata", {}).get("source_id", "untrusted"))
            if isinstance(raw.get("marketplace_metadata"), Mapping)
            else "untrusted",
            "provenance": "catalog row failed contract validation",
        }
        repaired["risk_verdict"] = _fail_closed_verdict(
            ExtensionRiskReason.MISSING_PROVENANCE,
            f"manifest validation failed for {extension_id}: {exc}",
        ).to_dict()
        return ExtensionManifest.from_mapping(repaired)

    evaluated = evaluate_manifest_risk(manifest, manually_selected=not manifest.disabled_by_default)
    if evaluated != manifest.risk_verdict:
        manifest = ExtensionManifest(
            extension_id=manifest.extension_id,
            source_kind=manifest.source_kind,
            version=manifest.version,
            compatibility=manifest.compatibility,
            declared_tools=manifest.declared_tools,
            requested_secrets=manifest.requested_secrets,
            stdio=manifest.stdio,
            network=manifest.network,
            filesystem=manifest.filesystem,
            dependencies=manifest.dependencies,
            dependency_findings=manifest.dependency_findings,
            overlap_categories=manifest.overlap_categories,
            destructive_capabilities=manifest.destructive_capabilities,
            authority_owner=manifest.authority_owner,
            risk_verdict=evaluated,
            disabled_by_default=evaluated.disabled_by_default,
            marketplace_metadata=manifest.marketplace_metadata,
            shell_capable=manifest.shell_capable,
            import_paths=manifest.import_paths,
            package_pin=manifest.package_pin,
            oauth=manifest.oauth,
        )
    return manifest


def _load_uncached(path: Path) -> tuple[ExtensionManifest, ...]:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ExtensionMarketplaceError(f"cannot read extension marketplace catalog {path}: {exc}") from exc
    if not isinstance(doc, Mapping):
        raise ExtensionMarketplaceError(f"extension marketplace catalog {path} must be a mapping")
    if doc.get("schema_version") != _CATALOG_SCHEMA_VERSION:
        raise ExtensionMarketplaceError(
            f"extension marketplace schema mismatch: expected {_CATALOG_SCHEMA_VERSION}, got {doc.get('schema_version')!r}"
        )
    raw_extensions = doc.get("extensions")
    if not isinstance(raw_extensions, Sequence) or isinstance(raw_extensions, (str, bytes)) or not raw_extensions:
        raise ExtensionMarketplaceError("extension marketplace must contain non-empty extensions")

    rows: list[ExtensionManifest] = []
    seen: set[str] = set()
    for raw in raw_extensions:
        if not isinstance(raw, Mapping):
            raise ExtensionMarketplaceError("extension catalog rows must be mappings")
        manifest = _manifest_from_mapping(raw)
        if manifest.extension_id in seen:
            raise ExtensionMarketplaceError(f"duplicate extension id {manifest.extension_id}")
        seen.add(manifest.extension_id)
        rows.append(manifest)
    return tuple(rows)


def load_extension_marketplace(path: Path | str | None = None) -> tuple[ExtensionManifest, ...]:
    """Load curated extension marketplace rows without mutating runtime config.

    Returns:
        Resolved extension marketplace value.
    """
    global _CATALOG_CACHE
    if path is not None:
        return _load_uncached(Path(path))
    with _CATALOG_LOCK:
        if _CATALOG_CACHE is None:
            _CATALOG_CACHE = _load_uncached(_CATALOG_PATH)
        return _CATALOG_CACHE


def reset_extension_marketplace_for_test() -> None:
    """Clear the module-level marketplace cache for deterministic tests."""
    global _CATALOG_CACHE
    with _CATALOG_LOCK:
        _CATALOG_CACHE = None


class ExtensionMarketplaceService:
    """Read and evaluate marketplace metadata while preserving Workbench authority."""

    def __init__(self, *, catalog_path: Path | str | None = None) -> None:
        self._catalog_path = Path(catalog_path) if catalog_path is not None else None

    def list_extensions(self) -> list[dict[str, Any]]:
        return [manifest.to_dict() for manifest in load_extension_marketplace(self._catalog_path)]

    def get_extension(self, extension_id: str) -> ExtensionManifest:
        """Execute the get extension operation.

        Returns:
            Resolved extension value.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        for manifest in load_extension_marketplace(self._catalog_path):
            if manifest.extension_id == extension_id:
                return manifest
        raise ExtensionMarketplaceError(f"extension {extension_id!r} not found")

    def import_metadata(self, payload: Mapping[str, Any]) -> ExtensionManifest:
        """Evaluate imported metadata only; no MCP/plugin config is modified."""
        return _manifest_from_mapping(payload)

    def evaluate_extension(self, extension_id: str, *, manually_selected: bool = False) -> ExtensionRiskVerdict:
        return evaluate_manifest_risk(self.get_extension(extension_id), manually_selected=manually_selected)

    def create_oauth_authorization_request(
        self,
        extension_id: str,
        *,
        redirect_uri: str,
        client_id: str,
        state: str | None = None,
        code_verifier: str | None = None,
    ) -> OAuthAuthorizationRequest:
        """Create a PKCE OAuth authorization URL for an MCP marketplace row.

        Returns:
            Value produced for the caller.

        Raises:
            ExtensionMarketplaceError: Propagated when validation, persistence, or execution fails.
        """
        manifest = self.get_extension(extension_id)
        if manifest.oauth is None:
            raise ExtensionMarketplaceError(f"extension {extension_id!r} has no OAuth metadata")
        verifier = code_verifier or secrets.token_urlsafe(48)
        challenge = _pkce_s256_challenge(verifier)
        request_state = state or secrets.token_urlsafe(24)
        query = urlencode({
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(manifest.oauth.scopes),
            "state": request_state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        separator = "&" if "?" in manifest.oauth.authorization_url else "?"
        return OAuthAuthorizationRequest(
            extension_id=manifest.extension_id,
            provider=manifest.oauth.provider,
            authorization_url=f"{manifest.oauth.authorization_url}{separator}{query}",
            state=request_state,
            code_verifier=verifier,
            code_challenge=challenge,
            scopes=manifest.oauth.scopes,
            redirect_uri=redirect_uri,
            client_id=client_id,
        )

    def exchange_oauth_authorization_code(
        self,
        extension_id: str,
        *,
        code: str,
        redirect_uri: str,
        client_id: str,
        code_verifier: str,
    ) -> OAuthTokenExchangeResult:
        """Exchange a PKCE authorization code for a redacting token result.

        Args:
            extension_id: Marketplace row receiving the OAuth token.
            code: Authorization code returned by the provider callback.
            redirect_uri: Redirect URI used in the authorization request.
            client_id: OAuth public client identifier.
            code_verifier: Original PKCE verifier paired with the authorization
                request's S256 challenge.

        Returns:
            Secret-bearing token result whose public serialization redacts token
            values and whose helper can explicitly produce Authorization headers.

        Raises:
            ExtensionMarketplaceError: If the row has no OAuth metadata, required
            PKCE material is missing, the provider fails, the provider response is
            not JSON, or the response omits ``access_token``.
        """
        manifest = self.get_extension(extension_id)
        if manifest.oauth is None:
            raise ExtensionMarketplaceError(f"extension {extension_id!r} has no OAuth metadata")

        form = {
            "grant_type": "authorization_code",
            "code": _required_oauth_text(code, "code"),
            "redirect_uri": _required_oauth_text(redirect_uri, "redirect_uri"),
            "client_id": _required_oauth_text(client_id, "client_id"),
            "code_verifier": _required_oauth_text(code_verifier, "code_verifier"),
        }
        headers = {
            "Accept": "application/json",
            "Content-Type": _OAUTH_FORM_CONTENT_TYPE,
        }
        try:
            response = requests.post(
                manifest.oauth.token_url,
                data=form,
                headers=headers,
                timeout=_OAUTH_TOKEN_TIMEOUT_SECONDS,
            )
        except requests.exceptions.RequestException as exc:
            raise ExtensionMarketplaceError(
                f"OAuth provider {manifest.oauth.provider!r} token exchange request failed"
            ) from exc

        if not 200 <= response.status_code < 300:
            raise ExtensionMarketplaceError(
                f"OAuth provider {manifest.oauth.provider!r} rejected token exchange with HTTP {response.status_code}"
            )

        try:
            raw_token = response.json()
        except ValueError as exc:
            raise ExtensionMarketplaceError(
                f"OAuth provider {manifest.oauth.provider!r} token response was not valid JSON"
            ) from exc
        if not isinstance(raw_token, Mapping):
            raise ExtensionMarketplaceError(
                f"OAuth provider {manifest.oauth.provider!r} token response must be a JSON object"
            )

        access_token = _required_oauth_text(raw_token.get("access_token"), "access_token")
        refresh_token = _optional_oauth_text(raw_token.get("refresh_token"), "refresh_token")
        token_type = _optional_oauth_text(raw_token.get("token_type"), "token_type") or "Bearer"
        return OAuthTokenExchangeResult(
            extension_id=manifest.extension_id,
            provider=manifest.oauth.provider,
            token_type=_normalize_oauth_token_type(token_type),
            access_token=access_token,
            refresh_token=refresh_token,
            scopes=_oauth_token_scopes(raw_token.get("scope"), manifest.oauth.scopes),
            expires_in_seconds=_oauth_expires_in(raw_token.get("expires_in"), manifest.oauth.provider),
        )


def _pkce_s256_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _required_oauth_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExtensionMarketplaceError(f"OAuth token exchange requires non-empty {field_name}")
    return value.strip()


def _optional_oauth_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ExtensionMarketplaceError(f"OAuth token response field {field_name} must be a non-empty string")
    return value.strip()


def _normalize_oauth_token_type(token_type: str) -> str:
    if token_type.lower() == "bearer":
        return "Bearer"
    return token_type


def _oauth_token_scopes(raw_scope: object, fallback: tuple[str, ...]) -> tuple[str, ...]:
    if raw_scope is None:
        return fallback
    if isinstance(raw_scope, str):
        scopes = tuple(scope for scope in raw_scope.split(" ") if scope)
    elif isinstance(raw_scope, Sequence) and not isinstance(raw_scope, (bytes, bytearray)):
        scopes = tuple(str(scope).strip() for scope in raw_scope if str(scope).strip())
    else:
        raise ExtensionMarketplaceError("OAuth token response field scope must be a string or list of strings")
    if not scopes:
        raise ExtensionMarketplaceError("OAuth token response field scope must not be empty when supplied")
    return scopes


def _oauth_expires_in(raw_expires_in: object, provider: str) -> int | None:
    if raw_expires_in is None:
        return None
    try:
        expires_in = int(raw_expires_in)
    except (TypeError, ValueError) as exc:
        raise ExtensionMarketplaceError(
            f"OAuth provider {provider!r} token response field expires_in must be an integer"
        ) from exc
    if expires_in < 0:
        raise ExtensionMarketplaceError(
            f"OAuth provider {provider!r} token response field expires_in must not be negative"
        )
    return expires_in


__all__ = [
    "ExtensionMarketplaceError",
    "ExtensionMarketplaceService",
    "OAuthAuthorizationRequest",
    "OAuthTokenExchangeResult",
    "load_extension_marketplace",
    "reset_extension_marketplace_for_test",
]
