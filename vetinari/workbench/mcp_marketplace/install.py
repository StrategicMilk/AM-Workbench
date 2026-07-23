"""Extension Marketplace install flow: catalog lookup, signature verification, registration.

This is the entry point for the end-to-end install pipeline:
  Catalog lookup -> Signature/admission check -> MCP transport probe -> Registry registration

Usage::

    from vetinari.workbench.mcp_marketplace.install import install_extension
    result = install_extension("my-extension", dry_run=True)

CLI entry point: ``python -m vetinari.workbench.mcp_marketplace install <extension_id>``
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from vetinari.workbench._contracts_enforcement import ContractViolation, strict_probe_or_raise
from vetinari.workbench.extensions.contracts import ExtensionManifest, ExtensionSourceKind
from vetinari.workbench.mcp_marketplace.catalog import (
    ExtensionMarketplaceError,
    ExtensionMarketplaceService,
    OAuthTokenExchangeResult,
)
from vetinari.workbench.mcp_marketplace.release import ExtensionReleaseService
from vetinari.workbench.mcp_marketplace.streamable_http import (
    StreamableHttpClient,
    StreamableHttpRequest,
)

logger = logging.getLogger(__name__)


# -- Exceptions ---------------------------------------------------------------


class ExtensionInstallError(RuntimeError):
    """Base exception for all install-flow failures."""


class ExtensionNotFoundError(ExtensionInstallError):
    """Raised when the requested extension is not in the marketplace catalog."""

    def __init__(self, extension_id: str) -> None:
        super().__init__(
            f"Extension {extension_id!r} was not found in the marketplace catalog. "
            "Check the extension ID and ensure the catalog is up to date."
        )
        self.extension_id = extension_id


class SignatureVerificationError(ExtensionInstallError):
    """Raised when the extension release package fails admission checks."""

    def __init__(self, extension_id: str, detail: str = "") -> None:
        msg = (
            f"Extension {extension_id!r} failed signature/admission verification "
            "and cannot be installed â€” the install has been blocked to protect workbench integrity."
        )
        if detail:
            msg = f"{msg} Detail: {detail}"
        super().__init__(msg)
        self.extension_id = extension_id


class MCPTransportProbeError(ExtensionInstallError):
    """Raised when an MCP server transport probe fails during install."""

    def __init__(self, extension_id: str, source_url: str, detail: str = "") -> None:
        msg = f"Extension {extension_id!r} MCP transport probe failed for endpoint {source_url!r}"
        if detail:
            msg = f"{msg}: {detail}"
        super().__init__(msg)
        self.extension_id = extension_id
        self.source_url = source_url


# -- Result dataclass ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstallResult:
    """Immutable outcome record for an extension install attempt.

    Attributes:
        extension_id: Marketplace identifier of the extension.
        status: One of ``"dry-run-ok"``, ``"installed"``.
        license_id: SPDX license identifier from the manifest, or ``None`` if
            the catalog entry carried no license information.
        error: Human-readable error string when status indicates partial failure.
            Not set on success paths.
    """

    extension_id: str  # marketplace ID
    status: str  # "dry-run-ok" | "installed"
    license_id: str | None = None  # SPDX identifier from manifest
    error: str | None = None  # advisory error detail (not set on success)
    oauth_authorized: bool = False  # true when an OAuth token was exchanged or supplied for install probing

    def __repr__(self) -> str:
        return (
            f"InstallResult(extension_id={self.extension_id!r}, status={self.status!r}, license_id={self.license_id!r})"
        )


# -- Integration helpers ------------------------------------------------------


def _get_catalog_entry(
    extension_id: str,
    *,
    service: ExtensionMarketplaceService | None = None,
) -> ExtensionManifest | None:
    """Look up an extension manifest from the marketplace catalog.

    Returns ``None`` when the extension is absent so callers can raise the
    typed :class:`ExtensionNotFoundError` rather than catching
    :class:`ExtensionMarketplaceError`.

    Args:
        extension_id: Marketplace identifier to look up.
        service: Optional pre-constructed service (used by tests for injection).

    Returns:
        The manifest if found, ``None`` otherwise.
    """
    svc = service or ExtensionMarketplaceService()
    try:
        return svc.get_extension(extension_id)
    except ExtensionMarketplaceError:
        logger.warning(
            "Catalog lookup for %r returned no entry â€” extension not found in marketplace",
            extension_id,
        )
        return None


def _verify_release_signature(
    manifest: ExtensionManifest,
    *,
    release_service: ExtensionReleaseService | None = None,
) -> bool:
    """Verify a manifest release is admissible before install.

    Wraps :class:`ExtensionReleaseService` admission check, which validates
    the package manifest for portability and default-on status.

    Note: ``release.py`` exposes ``ExtensionReleaseService.admit()`` rather
    than a standalone ``verify_release_signature`` symbol. This helper adapts
    the manifest-level call surface to the admission API.
    See finding MR-R3-002 for the integration context.

    Args:
        manifest: Resolved :class:`ExtensionManifest` from the catalog.
        release_service: Optional pre-constructed service (used by tests).

    Returns:
        ``True`` when the release is admitted; ``False`` otherwise.
    """
    svc = release_service or ExtensionReleaseService()
    # Build a minimal package payload that the admission gate expects.
    # ``default_on=True`` signals that this is an operator-initiated install
    # request, not an ambient auto-enable, so the gate can admit the package.
    package_payload: dict[str, object] = {
        "manifest": manifest.to_dict(),
        "default_on": True,
    }
    decision = svc.admit(package_payload)
    if not decision.admitted:
        logger.warning(
            "Extension %r failed release admission â€” code=%s; install blocked",
            manifest.extension_id,
            decision.code,
        )
    return decision.admitted


def _probe_mcp_transport(
    manifest: ExtensionManifest,
    *,
    client: StreamableHttpClient | None = None,
    authorization_headers: dict[str, str] | None = None,
) -> None:
    """Probe the MCP Streamable HTTP transport endpoint for MCP_SERVER extensions.

    When the manifest identifies an ``MCP_SERVER`` extension with a non-empty
    ``source_url``, this function sends a ``tools/list`` probe via the
    Streamable HTTP transport (MCP Specification 2025-11-05 Â§3.2). The probe
    confirms the endpoint is reachable and speaks the Streamable HTTP protocol
    before registration proceeds. Non-MCP-server extensions or those without a
    ``source_url`` are skipped silently.

    The probe is best-effort: a failed probe logs a WARNING but does NOT block
    install.  The registration step is the authoritative admission gate. This
    probe exists to surface transport misconfigurations early.

    Args:
        manifest: Verified :class:`ExtensionManifest` from the catalog.
        client: Optional pre-constructed :class:`StreamableHttpClient` (for tests).
        authorization_headers: Optional headers for authenticated probes, such as
            OAuth bearer tokens acquired during marketplace install.

    Raises:
        Nothing â€” transport failures are logged and swallowed so they don't
        block an otherwise-valid install.
    """
    source_url = manifest.marketplace_metadata.source_url
    if not source_url:
        if manifest.source_kind is not ExtensionSourceKind.MCP_SERVER:
            return
        raise MCPTransportProbeError(manifest.extension_id, "", "MCP_SERVER manifest has no source_url")

    probe_client = client or StreamableHttpClient(source_url, headers=authorization_headers)
    probe_request = StreamableHttpRequest(
        method="tools/list",
        params={},
        request_id=f"install-probe-{manifest.extension_id}",
    )

    def send_probe() -> None:
        for frame in probe_client.send(probe_request):
            if frame.is_final:
                logger.info(
                    "MCP transport probe for %r succeeded â€” endpoint=%s status=%s",
                    manifest.extension_id,
                    source_url,
                    frame.status,
                )
                break

    try:
        strict_probe_or_raise(send_probe, extension_id=manifest.extension_id, endpoint=source_url)
    except ContractViolation as exc:
        logger.warning(
            "MCP Streamable HTTP transport probe for %r failed (endpoint=%s) â€” "
            "transport may be unreachable; install blocked",
            manifest.extension_id,
            source_url,
        )
        raise MCPTransportProbeError(manifest.extension_id, source_url, exc.detail) from exc


def _register_extension(
    manifest: ExtensionManifest,
) -> None:
    """Register a verified extension with the plugin runtime.

    Delegates to :class:`~vetinari.workbench.plugin_runtime.registration.PluginRegistrationService`
    and logs the outcome. Registration does not load or enable plugin code â€”
    it only records the verdict so the runtime can honour it.

    Args:
        manifest: Verified manifest to register.

    Raises:
        ExtensionInstallError: When registration returns a blocked verdict.
    """
    # Import here to avoid a circular dependency at module load time.
    # registration.py already imports from catalog.py, so a top-level import
    # in install.py would create a cycle.
    from vetinari.workbench.plugin_runtime.registration import (
        PluginRegistrationService,
        PluginRegistrationStatus,
    )

    svc = PluginRegistrationService()
    decision = svc.register_extension(manifest.extension_id, manually_selected=True)
    logger.info(
        "Extension %r registration verdict: status=%s enabled=%s",
        manifest.extension_id,
        decision.status.value,
        decision.enabled,
    )
    if decision.status is PluginRegistrationStatus.BLOCKED:
        raise ExtensionInstallError(
            f"Extension {manifest.extension_id!r} was blocked during registry registration "
            f"â€” reasons: {[r.value for r in decision.reasons]}. "
            "The extension is not active. Review the risk verdict and resubmit with manual_selection=True "
            "after clearing the flagged conditions."
        )


def _resolve_oauth_token_for_install(
    manifest: ExtensionManifest,
    *,
    service: ExtensionMarketplaceService,
    oauth_token: OAuthTokenExchangeResult | None,
    oauth_code: str | None,
    oauth_redirect_uri: str | None,
    oauth_client_id: str | None,
    oauth_code_verifier: str | None,
) -> OAuthTokenExchangeResult | None:
    """Return an OAuth token for install-time MCP probing when requested."""
    supplied_material = (
        oauth_token is not None
        or oauth_code is not None
        or oauth_redirect_uri is not None
        or oauth_client_id is not None
        or oauth_code_verifier is not None
    )
    if not supplied_material:
        return None
    if manifest.oauth is None:
        raise ExtensionInstallError(f"Extension {manifest.extension_id!r} has no OAuth metadata for install")
    if oauth_token is not None:
        return oauth_token
    missing = [
        field
        for field, value in (
            ("oauth_code", oauth_code),
            ("oauth_redirect_uri", oauth_redirect_uri),
            ("oauth_client_id", oauth_client_id),
            ("oauth_code_verifier", oauth_code_verifier),
        )
        if not isinstance(value, str) or not value.strip()
    ]
    if missing:
        raise ExtensionInstallError(f"Extension {manifest.extension_id!r} OAuth install requires {', '.join(missing)}")
    try:
        return service.exchange_oauth_authorization_code(
            manifest.extension_id,
            code=str(oauth_code),
            redirect_uri=str(oauth_redirect_uri),
            client_id=str(oauth_client_id),
            code_verifier=str(oauth_code_verifier),
        )
    except ExtensionMarketplaceError as exc:
        raise ExtensionInstallError(f"Extension {manifest.extension_id!r} OAuth token exchange failed: {exc}") from exc


# -- Public API ---------------------------------------------------------------


def install_extension(
    extension_id: str,
    dry_run: bool = False,
    *,
    catalog_service: ExtensionMarketplaceService | None = None,
    release_service: ExtensionReleaseService | None = None,
    transport_client: StreamableHttpClient | None = None,
    oauth_token: OAuthTokenExchangeResult | None = None,
    oauth_code: str | None = None,
    oauth_redirect_uri: str | None = None,
    oauth_client_id: str | None = None,
    oauth_code_verifier: str | None = None,
) -> InstallResult:
    """Install an extension from the marketplace catalog end-to-end.

    The install pipeline is:
    1. Resolve the catalog entry for ``extension_id``.
    2. Verify the release package passes admission checks.
    3. Probe the MCP Streamable HTTP transport endpoint (MCP_SERVER extensions only).
    4. Register the extension with the plugin runtime (skipped on ``dry_run``).

    Args:
        extension_id: Marketplace identifier for the extension to install.
        dry_run: When ``True``, run steps 1-3 but skip registration.
            Returns status ``"dry-run-ok"`` on success.
        catalog_service: Optional pre-constructed catalog service (for tests).
        release_service: Optional pre-constructed release service (for tests).
        transport_client: Optional pre-constructed :class:`StreamableHttpClient`
            for the MCP transport probe (for tests).
        oauth_token: Optional pre-exchanged OAuth token for authenticated MCP
            Streamable HTTP probes. Public serialization remains redacted.
        oauth_code: Authorization code to exchange for an install probe token.
        oauth_redirect_uri: Redirect URI used by the authorization request.
        oauth_client_id: OAuth public client id used by the authorization
            request.
        oauth_code_verifier: PKCE verifier paired with the authorization
            request's S256 challenge.

    Returns:
        :class:`InstallResult` with status ``"dry-run-ok"`` or ``"installed"``.

    Raises:
        ExtensionNotFoundError: The extension ID is not present in the catalog.
        SignatureVerificationError: The release admission check failed.
        ExtensionInstallError: Registration was blocked (non-dry-run only).
    """
    catalog = catalog_service or ExtensionMarketplaceService()
    manifest = _get_catalog_entry(extension_id, service=catalog)
    if manifest is None:
        raise ExtensionNotFoundError(extension_id)

    install_token = _resolve_oauth_token_for_install(
        manifest,
        service=catalog,
        oauth_token=oauth_token,
        oauth_code=oauth_code,
        oauth_redirect_uri=oauth_redirect_uri,
        oauth_client_id=oauth_client_id,
        oauth_code_verifier=oauth_code_verifier,
    )

    admitted = _verify_release_signature(manifest, release_service=release_service)
    if not admitted:
        raise SignatureVerificationError(extension_id)

    _probe_mcp_transport(
        manifest,
        client=transport_client,
        authorization_headers=install_token.authorization_headers() if install_token is not None else None,
    )

    if dry_run:
        logger.info(
            "Dry-run install for extension %r passed admission â€” skipping registration",
            extension_id,
        )
        return InstallResult(
            extension_id=extension_id,
            status="dry-run-ok",
            license_id=manifest.license_id or None,
            oauth_authorized=install_token is not None,
        )

    _register_extension(manifest)
    logger.info("Extension %r installed successfully", extension_id)
    return InstallResult(
        extension_id=extension_id,
        status="installed",
        license_id=manifest.license_id or None,
        oauth_authorized=install_token is not None,
    )


__all__ = [
    "ExtensionInstallError",
    "ExtensionNotFoundError",
    "InstallResult",
    "MCPTransportProbeError",
    "SignatureVerificationError",
    "install_extension",
]
