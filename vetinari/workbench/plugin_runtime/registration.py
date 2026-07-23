"""Timeout-bounded, fail-closed plugin registration decisions."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from vetinari.utils.bounded_collections import BoundedDict, BoundedList
from vetinari.workbench.adapters.authority import AuthorityMode
from vetinari.workbench.extensions.contracts import (
    ExtensionManifest,
    ExtensionRiskReason,
    ExtensionRiskStatus,
    ExtensionRiskVerdict,
    evaluate_manifest_risk,
)
from vetinari.workbench.mcp_marketplace.catalog import ExtensionMarketplaceService
from vetinari.workbench.plugin_runtime.sandbox import PluginSandboxScan, PluginSandboxScanner, PluginSandboxStatus
from vetinari.workbench.tool_trust.contracts import ToolTrustStatus

logger = logging.getLogger(__name__)


class PluginRegistrationStatus(str, Enum):
    """Registration status emitted before a plugin can inherit authority."""

    ENABLED = "enabled"
    BLOCKED = "blocked"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class PluginRegistrationDecision:
    """Registration verdict for one extension."""

    extension_id: str
    status: PluginRegistrationStatus
    enabled: bool
    reasons: tuple[ExtensionRiskReason, ...]
    risk_verdict: ExtensionRiskVerdict
    sandbox: Mapping[str, Any]
    manual_selection: bool
    tool_surface_status: str
    adapter_authority_mode: str
    secrets: tuple[dict[str, Any], ...]
    details: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "extension_id": self.extension_id,
            "status": self.status.value,
            "enabled": self.enabled,
            "reasons": [reason.value for reason in self.reasons],
            "risk_verdict": self.risk_verdict.to_dict(),
            "sandbox": dict(self.sandbox),
            "manual_selection": self.manual_selection,
            "tool_surface_status": self.tool_surface_status,
            "adapter_authority_mode": self.adapter_authority_mode,
            "secrets": list(self.secrets),
            "details": dict(sorted(self.details.items())),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"PluginRegistrationDecision(extension_id={self.extension_id!r}, status={self.status!r}, enabled={self.enabled!r})"


_REGISTRATION_LOCK = threading.Lock()
_REGISTRATION_CACHE: BoundedDict[str, PluginRegistrationDecision] = BoundedDict(1_000)


def reset_plugin_registration_for_test() -> None:
    """Clear the module-level registration cache for deterministic tests."""
    with _REGISTRATION_LOCK:
        _REGISTRATION_CACHE.clear()


def _manifest_digest(manifest: ExtensionManifest) -> str:
    body = json.dumps(manifest.to_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class PluginRegistrationService:
    """Evaluate extension registration without mutating MCP or environment config."""

    def __init__(
        self,
        *,
        marketplace: ExtensionMarketplaceService | None = None,
        scanner: PluginSandboxScanner | None = None,
        timeout_seconds: float = 1.0,
        scan_timeout_runner: Callable[[PluginSandboxScanner, ExtensionManifest, str, float], PluginSandboxScan | None]
        | None = None,
    ) -> None:
        self._marketplace = marketplace or ExtensionMarketplaceService()
        self._scanner = scanner or PluginSandboxScanner()
        self._timeout_seconds = timeout_seconds
        self._scan_timeout_runner = scan_timeout_runner

    def evaluate_registration(
        self,
        extension_id: str,
        *,
        manually_selected: bool = False,
        source_text: str = "",
        tool_surface_available: bool = True,
        partial_state: bool = False,
    ) -> PluginRegistrationDecision:
        """Execute the evaluate registration operation.

        Returns:
            PluginRegistrationDecision value produced by evaluate_registration().
        """
        env_before = dict(os.environ)
        source_digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        manifest = self._marketplace.get_extension(extension_id)
        manifest_digest = _manifest_digest(manifest)
        cache_key = (
            f"{extension_id}:{manifest_digest}:{manually_selected}:"
            f"{tool_surface_available}:{partial_state}:{source_digest}"
        )
        with _REGISTRATION_LOCK:
            cached = _REGISTRATION_CACHE.get(cache_key)
            if cached is not None:
                return cached
        decision = self._build_decision(
            manifest,
            manually_selected=manually_selected,
            source_text=source_text,
            tool_surface_available=tool_surface_available,
            partial_state=partial_state,
        )
        if dict(os.environ) != env_before:
            decision = self._blocked_decision(
                manifest,
                (ExtensionRiskReason.PARTIAL_REGISTRATION,),
                "registration attempted to mutate process environment",
                manually_selected=manually_selected,
            )
        with _REGISTRATION_LOCK:
            existing = _REGISTRATION_CACHE.setdefault(cache_key, decision)
            return existing

    def register_extension(self, extension_id: str, *, manually_selected: bool = False) -> PluginRegistrationDecision:
        """Return a registration verdict; plugin code is not loaded or enabled here.

        Args:
            extension_id: Marketplace identifier for the extension to register.
            manually_selected: Whether the extension was explicitly chosen by the operator.

        Returns:
            PluginRegistrationDecision with the verdict and any advisory warnings.
        """
        try:
            manifest = self._marketplace.get_extension(extension_id)
        except Exception:
            # Unknown / unfetchable extension — let evaluate_registration handle
            # the failure path; SPDX license advisory is best-effort here.
            manifest = None
        if manifest is not None and not manifest.license_id.strip():
            # SPDX license advisory: not fail-closed; extension still registers.
            # Empty license_id surfaces a WARNING so operators can audit unlicensed
            # extensions without the registration gate rejecting them.
            logger.warning(
                "Registering extension %r with empty license_id — SPDX license identifier recommended",
                extension_id,
            )
        return self.evaluate_registration(extension_id, manually_selected=manually_selected)

    def _build_decision(
        self,
        manifest: ExtensionManifest,
        *,
        manually_selected: bool,
        source_text: str,
        tool_surface_available: bool,
        partial_state: bool,
    ) -> PluginRegistrationDecision:
        if partial_state:
            return self._blocked_decision(
                manifest,
                (ExtensionRiskReason.PARTIAL_REGISTRATION,),
                "partial registration state",
                manually_selected=manually_selected,
            )
        scan = self._scan_with_timeout(manifest, source_text)
        if scan is None:
            return self._blocked_decision(
                manifest,
                (ExtensionRiskReason.TIMEOUT,),
                "sandbox scan timeout",
                manually_selected=manually_selected,
            )
        risk_verdict = evaluate_manifest_risk(manifest, manually_selected=manually_selected)
        reasons = BoundedList[ExtensionRiskReason](
            16,
            risk_verdict.reasons if risk_verdict.status is not ExtensionRiskStatus.ALLOWED else (),
        )
        if not scan.allowed:
            reasons.extend(ExtensionRiskReason(finding["reason"]) for finding in scan.to_dict()["findings"])
        if not tool_surface_available:
            reasons.append(ExtensionRiskReason.MISSING_PIN)

        unique_reasons = tuple(dict.fromkeys(reasons))
        enabled = not unique_reasons and manually_selected and tool_surface_available
        status = PluginRegistrationStatus.ENABLED if enabled else PluginRegistrationStatus.BLOCKED
        return PluginRegistrationDecision(
            extension_id=manifest.extension_id,
            status=status,
            enabled=enabled,
            reasons=(ExtensionRiskReason.TRUSTED,) if enabled else unique_reasons,
            risk_verdict=risk_verdict,
            sandbox=scan.to_dict(),
            manual_selection=manually_selected,
            tool_surface_status=ToolTrustStatus.ALLOWED.value
            if tool_surface_available
            else ToolTrustStatus.BLOCKED.value,
            adapter_authority_mode=AuthorityMode.WORKBENCH_AUTHORITATIVE.value,
            secrets=tuple(secret.to_dict() for secret in manifest.requested_secrets),
            details={"registration": "enabled" if enabled else "blocked"},
        )

    def _scan_with_timeout(self, manifest: ExtensionManifest, source_text: str) -> PluginSandboxScan | None:
        if self._scan_timeout_runner is not None:
            return self._scan_timeout_runner(self._scanner, manifest, source_text, self._timeout_seconds)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self._scanner.scan_mapping, manifest.to_dict(), source_text=source_text)
            try:
                return future.result(timeout=self._timeout_seconds)
            except concurrent.futures.TimeoutError:
                logger.warning("Handled recoverable failure before fallback.", exc_info=True)
                future.cancel()
                return None

    @staticmethod
    def _blocked_decision(
        manifest: ExtensionManifest,
        reasons: tuple[ExtensionRiskReason, ...],
        detail: str,
        *,
        manually_selected: bool,
    ) -> PluginRegistrationDecision:
        recoverable_degradation = bool(reasons) and set(reasons).issubset({ExtensionRiskReason.TIMEOUT})
        risk_status = ExtensionRiskStatus.DEGRADED if recoverable_degradation else ExtensionRiskStatus.BLOCKED
        registration_status = (
            PluginRegistrationStatus.DEGRADED if recoverable_degradation else PluginRegistrationStatus.BLOCKED
        )
        sandbox_status = PluginSandboxStatus.DEGRADED if recoverable_degradation else PluginSandboxStatus.BLOCKED
        risk_verdict = ExtensionRiskVerdict(
            status=risk_status,
            allowed=False,
            reasons=reasons,
            disabled_by_default=True,
            manual_selection_required=True,
            details={"registration": detail},
        )
        return PluginRegistrationDecision(
            extension_id=manifest.extension_id,
            status=registration_status,
            enabled=False,
            reasons=reasons,
            risk_verdict=risk_verdict,
            sandbox={"status": sandbox_status.value, "allowed": False, "findings": [], "requested_secrets": []},
            manual_selection=manually_selected,
            tool_surface_status=ToolTrustStatus.BLOCKED.value,
            adapter_authority_mode=AuthorityMode.WORKBENCH_AUTHORITATIVE.value,
            secrets=tuple(secret.to_dict() for secret in manifest.requested_secrets),
            details={"registration": detail},
        )


__all__ = [
    "PluginRegistrationDecision",
    "PluginRegistrationService",
    "PluginRegistrationStatus",
    "reset_plugin_registration_for_test",
]
