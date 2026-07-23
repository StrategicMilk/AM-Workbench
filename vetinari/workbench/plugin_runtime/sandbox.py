"""Static sandbox policy checks for Workbench extensions."""

from __future__ import annotations

import ast
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from vetinari.workbench.extensions.contracts import ExtensionManifest, ExtensionRiskReason

logger = logging.getLogger(__name__)


FORBIDDEN_IMPORT_ROOTS = frozenset({"os", "subprocess", "socket", "shutil", "ctypes", "winreg"})
UNSAFE_DEPENDENCY_MARKERS = ("vulnerable", "unsafe", "conflict", "unreviewed")


class PluginSandboxStatus(str, Enum):
    """Static scan status for plugin code and metadata."""

    CLEAR = "clear"
    BLOCKED = "blocked"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class PluginSandboxFinding:
    """One fail-closed sandbox finding."""

    reason: ExtensionRiskReason
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"reason": self.reason.value, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class PluginSandboxScan:
    """Static sandbox scan result."""

    status: PluginSandboxStatus
    allowed: bool
    findings: tuple[PluginSandboxFinding, ...]
    requested_secrets: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "allowed": self.allowed,
            "findings": [finding.to_dict() for finding in self.findings],
            "requested_secrets": list(self.requested_secrets),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"PluginSandboxScan(status={self.status!r}, allowed={self.allowed!r}, findings={self.findings!r})"


class PluginSandboxScanner:
    """Evaluate plugin metadata and source text without executing plugin code."""

    def __init__(self, *, forbidden_imports: set[str] | frozenset[str] = FORBIDDEN_IMPORT_ROOTS) -> None:
        self._forbidden_imports = frozenset(forbidden_imports)

    def scan_manifest(
        self,
        manifest: ExtensionManifest,
        *,
        source_text: str = "",
        source_path: Path | str | None = None,
    ) -> PluginSandboxScan:
        """Execute the scan manifest operation.

        Returns:
            PluginSandboxScan value produced by scan_manifest().
        """
        findings: list[PluginSandboxFinding] = []
        if manifest.stdio or manifest.shell_capable:
            findings.append(PluginSandboxFinding(ExtensionRiskReason.SHELL_OR_STDIO, "stdio or shell-capable surface"))
        if manifest.dependency_findings or self._has_unsafe_dependency(manifest.dependencies):
            findings.append(PluginSandboxFinding(ExtensionRiskReason.UNSAFE_DEPENDENCY, "unsafe dependency finding"))
        findings.extend(
            PluginSandboxFinding(ExtensionRiskReason.UNSCOPED_CREDENTIAL, f"secret {secret.name} has no scope")
            for secret in manifest.requested_secrets
            if not secret.scope
        )

        text = source_text
        if source_path is not None:
            try:
                text = Path(source_path).read_text(encoding="utf-8")
            except OSError as exc:
                findings.append(
                    PluginSandboxFinding(ExtensionRiskReason.PARTIAL_REGISTRATION, f"source unreadable: {exc}")
                )
        if text:
            findings.extend(self._scan_forbidden_imports(text))

        blocked = bool(findings)
        return PluginSandboxScan(
            status=PluginSandboxStatus.BLOCKED if blocked else PluginSandboxStatus.CLEAR,
            allowed=not blocked,
            findings=tuple(dict.fromkeys(findings)),
            requested_secrets=tuple(secret.to_dict() for secret in manifest.requested_secrets),
        )

    def scan_mapping(self, manifest_payload: Mapping[str, Any], *, source_text: str = "") -> PluginSandboxScan:
        return self.scan_manifest(ExtensionManifest.from_mapping(manifest_payload), source_text=source_text)

    def _scan_forbidden_imports(self, source_text: str) -> tuple[PluginSandboxFinding, ...]:
        try:
            parsed = ast.parse(source_text)
        except SyntaxError as exc:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return (PluginSandboxFinding(ExtensionRiskReason.PARTIAL_REGISTRATION, f"source parse failed: {exc}"),)
        findings: list[PluginSandboxFinding] = []
        findings.extend(
            PluginSandboxFinding(ExtensionRiskReason.FORBIDDEN_IMPORT, f"forbidden import {root}")
            for node in ast.walk(parsed)
            for root in self._import_roots(node)
            if root in self._forbidden_imports
        )
        return tuple(dict.fromkeys(findings))

    @staticmethod
    def _import_roots(node: ast.AST) -> tuple[str, ...]:
        if isinstance(node, ast.Import):
            return tuple(alias.name.split(".", 1)[0] for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.module:
            return (node.module.split(".", 1)[0],)
        return ()

    @staticmethod
    def _has_unsafe_dependency(dependencies: Sequence[str]) -> bool:
        return any(any(marker in dep.lower() for marker in UNSAFE_DEPENDENCY_MARKERS) for dep in dependencies)


__all__ = [
    "FORBIDDEN_IMPORT_ROOTS",
    "PluginSandboxFinding",
    "PluginSandboxScan",
    "PluginSandboxScanner",
    "PluginSandboxStatus",
]
