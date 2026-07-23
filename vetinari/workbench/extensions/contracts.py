"""Immutable contracts for Workbench extension and MCP marketplace safety."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

SCHEMA_VERSION = "1.0"

NATIVE_OVERLAP_CATEGORIES = frozenset({
    "browser",
    "filesystem",
    "memory",
    "document",
    "channel",
    "shell",
    "network",
    "destructive",
})
FORBIDDEN_AUTHORITY_OWNERS = frozenset({"extension", "external", "mcp_server", "plugin"})
FORBIDDEN_IMPORT_PREFIXES = frozenset({"os.system", "subprocess", "ctypes"})
RISKY_IMPORT_PREFIXES = frozenset({"importlib", "socket", "shutil.rmtree", "pathlib.Path.unlink"})


class ExtensionContractError(ValueError):
    """Raised when extension metadata cannot be trusted."""


class ExtensionSourceKind(str, Enum):
    """Supported marketplace source categories."""

    MCP_SERVER = "mcp_server"
    PLUGIN = "plugin"
    CAPABILITY_PACK = "capability_pack"
    LOCAL_BUNDLE = "local_bundle"


class ExtensionRiskStatus(str, Enum):
    """Fail-closed marketplace risk status."""

    ALLOWED = "allowed"
    BLOCKED = "blocked"
    DEGRADED = "degraded"


class ExtensionRiskReason(str, Enum):
    """Machine-readable extension safety reasons."""

    TRUSTED = "trusted"
    RISKY_IMPORT = "risky_import"
    NATIVE_OVERLAP = "native_overlap"
    DESTRUCTIVE_CAPABILITY = "destructive_capability"
    SHELL_OR_STDIO = "shell_or_stdio"
    NETWORK_CAPABILITY = "network_capability"
    UNSCOPED_CREDENTIAL = "unscoped_secret"
    UNSAFE_DEPENDENCY = "unsafe_dependency"
    MISSING_PROVENANCE = "missing_provenance"
    MISSING_PIN = "missing_pin"
    MISSING_OAUTH = "missing_oauth"
    MISSING_COMPATIBILITY = "missing_compatibility"
    WORKBENCH_AUTHORITY_CONFLICT = "workbench_authority_conflict"
    MANUAL_SELECTION_REQUIRED = "manual_selection_required"
    FORBIDDEN_IMPORT = "forbidden_import"
    TIMEOUT = "timeout"
    PARTIAL_REGISTRATION = "partial_registration"


@dataclass(frozen=True, slots=True)
class SecretRequest:
    """A secret requested by an extension without exposing its value."""

    name: str
    scope: str
    required: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _required_text(self.name, "secret.name"))
        object.__setattr__(self, "scope", _required_text(self.scope, "secret.scope"))

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "scope": self.scope, "required": self.required, "value": "********"}


@dataclass(frozen=True, slots=True)
class MarketplaceMetadata:
    """Provenance preserved from curated or imported marketplace metadata."""

    source_id: str
    source_url: str = ""
    retrieved_at_utc: str = ""
    published_at_utc: str = ""
    package_pin: str = ""
    provenance: str = ""
    risk_tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _required_text(self.source_id, "metadata.source_id"))
        object.__setattr__(self, "risk_tags", _string_tuple(self.risk_tags, "metadata.risk_tags"))

    @property
    def has_provenance(self) -> bool:
        return bool(self.source_id and (self.source_url or self.provenance))

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_url": self.source_url,
            "retrieved_at_utc": self.retrieved_at_utc,
            "published_at_utc": self.published_at_utc,
            "package_pin": self.package_pin,
            "provenance": self.provenance,
            "risk_tags": list(self.risk_tags),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MarketplaceMetadata(source_id={self.source_id!r}, source_url={self.source_url!r}, retrieved_at_utc={self.retrieved_at_utc!r})"


@dataclass(frozen=True, slots=True)
class OAuthInstallMetadata:
    """Non-secret OAuth metadata required before MCP marketplace installation."""

    provider: str
    authorization_url: str
    token_url: str
    scopes: tuple[str, ...]
    pkce_required: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _required_text(self.provider, "oauth.provider"))
        object.__setattr__(self, "authorization_url", _required_text(self.authorization_url, "oauth.authorization_url"))
        object.__setattr__(self, "token_url", _required_text(self.token_url, "oauth.token_url"))
        object.__setattr__(self, "scopes", _string_tuple(self.scopes, "oauth.scopes"))
        if not self.scopes:
            raise ExtensionContractError("oauth.scopes must be non-empty")
        if not self.pkce_required:
            raise ExtensionContractError("oauth.pkce_required must be true for marketplace installs")

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "authorization_url": self.authorization_url,
            "token_url": self.token_url,
            "scopes": list(self.scopes),
            "pkce_required": self.pkce_required,
        }

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"provider={self.provider!r}, "
            f"authorization_url={self.authorization_url!r}, "
            f"token_url={self.token_url!r}, "
            f"scopes={self.scopes!r}"
            ")"
        )


@dataclass(frozen=True, slots=True)
class ExtensionRiskVerdict:
    """Current Workbench decision for an extension manifest."""

    status: ExtensionRiskStatus
    allowed: bool
    reasons: tuple[ExtensionRiskReason, ...]
    disabled_by_default: bool
    manual_selection_required: bool = True
    details: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", ExtensionRiskStatus(self.status))
        object.__setattr__(self, "reasons", tuple(ExtensionRiskReason(reason) for reason in self.reasons))
        if self.allowed and self.status is not ExtensionRiskStatus.ALLOWED:
            raise ExtensionContractError("allowed verdicts must use allowed status")
        if self.allowed and self.disabled_by_default:
            raise ExtensionContractError("disabled_by_default verdicts cannot be allowed")
        object.__setattr__(self, "details", {str(k): str(v) for k, v in self.details.items()})

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "allowed": self.allowed,
            "reasons": [reason.value for reason in self.reasons],
            "disabled_by_default": self.disabled_by_default,
            "manual_selection_required": self.manual_selection_required,
            "details": dict(sorted(self.details.items())),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ExtensionRiskVerdict(status={self.status!r}, allowed={self.allowed!r}, reasons={self.reasons!r})"


@dataclass(frozen=True, slots=True)
class ExtensionManifest:
    """Immutable extension manifest evaluated before runtime registration."""

    extension_id: str
    source_kind: ExtensionSourceKind
    version: str
    compatibility: str
    declared_tools: tuple[str, ...]
    requested_secrets: tuple[SecretRequest, ...]
    stdio: bool
    network: bool
    filesystem: bool
    dependencies: tuple[str, ...]
    dependency_findings: tuple[str, ...]
    overlap_categories: tuple[str, ...]
    destructive_capabilities: tuple[str, ...]
    authority_owner: str
    risk_verdict: ExtensionRiskVerdict
    disabled_by_default: bool
    marketplace_metadata: MarketplaceMetadata
    shell_capable: bool = False
    import_paths: tuple[str, ...] = ()
    package_pin: str = ""
    oauth: OAuthInstallMetadata | None = None
    schema_version: str = SCHEMA_VERSION
    # SPDX license identifier for this extension. Defaults to the project
    # license (Apache-2.0). Empty string ("") is allowed but logs a WARNING
    # at registration time so unlicensed extensions are visible to operators
    # without blocking the registration (advisory, not fail-closed).
    license_id: str = "Apache-2.0"

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ExtensionContractError(f"schema_version must be {SCHEMA_VERSION}")
        object.__setattr__(self, "extension_id", _required_text(self.extension_id, "extension_id"))
        object.__setattr__(self, "source_kind", ExtensionSourceKind(self.source_kind))
        object.__setattr__(self, "version", _required_text(self.version, "version"))
        object.__setattr__(self, "compatibility", _required_text(self.compatibility, "compatibility"))
        object.__setattr__(self, "declared_tools", _string_tuple(self.declared_tools, "declared_tools"))
        object.__setattr__(self, "dependencies", _string_tuple(self.dependencies, "dependencies"))
        object.__setattr__(self, "dependency_findings", _string_tuple(self.dependency_findings, "dependency_findings"))
        object.__setattr__(self, "overlap_categories", _string_tuple(self.overlap_categories, "overlap_categories"))
        object.__setattr__(
            self,
            "destructive_capabilities",
            _string_tuple(self.destructive_capabilities, "destructive_capabilities"),
        )
        object.__setattr__(self, "authority_owner", _required_text(self.authority_owner, "authority_owner"))
        object.__setattr__(self, "import_paths", _string_tuple(self.import_paths, "import_paths"))
        object.__setattr__(self, "requested_secrets", tuple(self.requested_secrets))
        object.__setattr__(self, "risk_verdict", self.risk_verdict)
        if self.authority_owner.lower() in FORBIDDEN_AUTHORITY_OWNERS:
            raise ExtensionContractError("extensions cannot declare themselves authoritative over Workbench")
        if self.risk_verdict.disabled_by_default != self.disabled_by_default:
            raise ExtensionContractError("manifest disabled_by_default must match risk verdict")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> ExtensionManifest:
        """Build a manifest from a schema/config-shaped mapping.

        Returns:
            ExtensionManifest value produced by from_mapping().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        risk = raw.get("risk_verdict", {})
        if not isinstance(risk, Mapping):
            raise ExtensionContractError("risk_verdict must be a mapping")
        metadata = raw.get("marketplace_metadata", {})
        if not isinstance(metadata, Mapping):
            raise ExtensionContractError("marketplace_metadata must be a mapping")
        oauth = raw.get("oauth")
        if oauth is not None and not isinstance(oauth, Mapping):
            raise ExtensionContractError("oauth must be a mapping when provided")
        secrets = raw.get("requested_secrets", ())
        if isinstance(secrets, (str, bytes)) or not isinstance(secrets, Sequence):
            raise ExtensionContractError("requested_secrets must be a list")
        for index, item in enumerate(secrets):
            if not isinstance(item, Mapping):
                raise ExtensionContractError(f"requested_secrets[{index}] must be a mapping")
        return cls(
            schema_version=str(raw.get("schema_version", SCHEMA_VERSION)),
            extension_id=str(raw.get("extension_id", "")),
            source_kind=ExtensionSourceKind(str(raw.get("source_kind", ""))),
            version=str(raw.get("version", "")),
            compatibility=str(raw.get("compatibility", "")),
            declared_tools=_string_tuple(raw.get("declared_tools", ()), "declared_tools"),
            requested_secrets=tuple(
                SecretRequest(
                    name=str(item.get("name", "")),
                    scope=str(item.get("scope", "")),
                    required=bool(item.get("required", True)),
                )
                for item in secrets
                if isinstance(item, Mapping)
            ),
            stdio=bool(raw.get("stdio", False)),
            network=bool(raw.get("network", False)),
            filesystem=bool(raw.get("filesystem", False)),
            dependencies=_string_tuple(raw.get("dependencies", ()), "dependencies"),
            dependency_findings=_string_tuple(raw.get("dependency_findings", ()), "dependency_findings"),
            overlap_categories=_string_tuple(raw.get("overlap_categories", ()), "overlap_categories"),
            destructive_capabilities=_string_tuple(raw.get("destructive_capabilities", ()), "destructive_capabilities"),
            authority_owner=str(raw.get("authority_owner", "")),
            risk_verdict=ExtensionRiskVerdict(
                status=ExtensionRiskStatus(str(risk.get("status", ExtensionRiskStatus.BLOCKED.value))),
                allowed=bool(risk.get("allowed", False)),
                reasons=tuple(ExtensionRiskReason(str(reason)) for reason in risk.get("reasons", ())),
                disabled_by_default=bool(risk.get("disabled_by_default", True)),
                manual_selection_required=bool(risk.get("manual_selection_required", True)),
                details={str(k): str(v) for k, v in dict(risk.get("details", {})).items()},
            ),
            disabled_by_default=bool(raw.get("disabled_by_default", True)),
            marketplace_metadata=MarketplaceMetadata(
                source_id=str(metadata.get("source_id", "")),
                source_url=str(metadata.get("source_url", "")),
                retrieved_at_utc=str(metadata.get("retrieved_at_utc", "")),
                published_at_utc=str(metadata.get("published_at_utc", "")),
                package_pin=str(metadata.get("package_pin", "")),
                provenance=str(metadata.get("provenance", "")),
                risk_tags=_string_tuple(metadata.get("risk_tags", ()), "metadata.risk_tags"),
            ),
            shell_capable=bool(raw.get("shell_capable", False)),
            import_paths=_string_tuple(raw.get("import_paths", ()), "import_paths"),
            package_pin=str(raw.get("package_pin", "")),
            oauth=OAuthInstallMetadata(
                provider=str(oauth.get("provider", "")),
                authorization_url=str(oauth.get("authorization_url", "")),
                token_url=str(oauth.get("token_url", "")),
                scopes=_string_tuple(oauth.get("scopes", ()), "oauth.scopes"),
                pkce_required=bool(oauth.get("pkce_required", True)),
            )
            if isinstance(oauth, Mapping)
            else None,
            license_id=str(raw.get("license_id", "Apache-2.0")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        row = asdict(self)
        row["source_kind"] = self.source_kind.value
        row["risk_verdict"] = self.risk_verdict.to_dict()
        row["marketplace_metadata"] = self.marketplace_metadata.to_dict()
        row["requested_secrets"] = [secret.to_dict() for secret in self.requested_secrets]
        row["oauth"] = self.oauth.to_dict() if self.oauth is not None else None
        return row

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ExtensionManifest(extension_id={self.extension_id!r}, source_kind={self.source_kind!r}, version={self.version!r})"


def evaluate_manifest_risk(manifest: ExtensionManifest, *, manually_selected: bool = False) -> ExtensionRiskVerdict:
    """Evaluate intrinsic manifest risk without executing plugin code.

    Returns:
        ExtensionRiskVerdict value produced by evaluate_manifest_risk().
    """
    reasons: list[ExtensionRiskReason] = []
    if not manifest.marketplace_metadata.has_provenance:
        reasons.append(ExtensionRiskReason.MISSING_PROVENANCE)
    if manifest.source_kind is ExtensionSourceKind.MCP_SERVER and manifest.stdio and not manifest.package_pin:
        reasons.append(ExtensionRiskReason.MISSING_PIN)
    if (
        manifest.source_kind is ExtensionSourceKind.MCP_SERVER
        and (manifest.network or manifest.requested_secrets)
        and manifest.oauth is None
    ):
        reasons.append(ExtensionRiskReason.MISSING_OAUTH)
    if not manifest.compatibility:
        reasons.append(ExtensionRiskReason.MISSING_COMPATIBILITY)
    if manifest.stdio or manifest.shell_capable:
        reasons.append(ExtensionRiskReason.SHELL_OR_STDIO)
    if manifest.network:
        reasons.append(ExtensionRiskReason.NETWORK_CAPABILITY)
    if manifest.destructive_capabilities:
        reasons.append(ExtensionRiskReason.DESTRUCTIVE_CAPABILITY)
    if set(manifest.overlap_categories) & NATIVE_OVERLAP_CATEGORIES:
        reasons.append(ExtensionRiskReason.NATIVE_OVERLAP)
    if any(not secret.scope for secret in manifest.requested_secrets):
        reasons.append(ExtensionRiskReason.UNSCOPED_CREDENTIAL)
    if manifest.dependency_findings:
        reasons.append(ExtensionRiskReason.UNSAFE_DEPENDENCY)
    if manifest.authority_owner.lower() != "workbench":
        reasons.append(ExtensionRiskReason.WORKBENCH_AUTHORITY_CONFLICT)
    for import_path in manifest.import_paths:
        normalized = import_path.strip()
        if any(normalized == prefix or normalized.startswith(f"{prefix}.") for prefix in FORBIDDEN_IMPORT_PREFIXES):
            reasons.append(ExtensionRiskReason.FORBIDDEN_IMPORT)
        elif any(normalized == prefix or normalized.startswith(f"{prefix}.") for prefix in RISKY_IMPORT_PREFIXES):
            reasons.append(ExtensionRiskReason.RISKY_IMPORT)
    if manifest.disabled_by_default and not manually_selected:
        reasons.append(ExtensionRiskReason.MANUAL_SELECTION_REQUIRED)

    unique_reasons = tuple(dict.fromkeys(reasons))
    allowed = not unique_reasons
    return ExtensionRiskVerdict(
        status=ExtensionRiskStatus.ALLOWED if allowed else ExtensionRiskStatus.BLOCKED,
        allowed=allowed,
        reasons=(ExtensionRiskReason.TRUSTED,) if allowed else unique_reasons,
        disabled_by_default=not allowed,
        manual_selection_required=not manually_selected or bool(unique_reasons),
    )


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExtensionContractError(f"{field_name} must be non-empty")
    return value.strip()


def _string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ExtensionContractError(f"{field_name} must be a list of strings")
    rows = tuple(str(item).strip() for item in value if str(item).strip())
    if len(rows) != len(set(rows)):
        raise ExtensionContractError(f"{field_name} must not contain duplicates")
    return rows


__all__ = [
    "FORBIDDEN_IMPORT_PREFIXES",
    "NATIVE_OVERLAP_CATEGORIES",
    "RISKY_IMPORT_PREFIXES",
    "SCHEMA_VERSION",
    "ExtensionContractError",
    "ExtensionManifest",
    "ExtensionRiskReason",
    "ExtensionRiskStatus",
    "ExtensionRiskVerdict",
    "ExtensionSourceKind",
    "MarketplaceMetadata",
    "OAuthInstallMetadata",
    "SecretRequest",
    "evaluate_manifest_risk",
]
