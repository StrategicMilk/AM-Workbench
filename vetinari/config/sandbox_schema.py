"""Pydantic validation schemas for ``config/sandbox_policy.yaml``.

Defines subprocess and external sandbox configurations, permission rules,
approval gates, and the top-level :class:`SandboxPolicyConfig`.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from vetinari.config.models_schema import _load_yaml
from vetinari.constants import _PROJECT_ROOT, SANDBOX_MAX_MEMORY_MB, SANDBOX_TIMEOUT

logger = logging.getLogger(__name__)


class SubprocessSandbox(BaseModel):
    """Configuration for the subprocess (CodeSandbox) execution backend.

    Attributes:
        enabled: Whether the subprocess sandbox is active.
        timeout_seconds: Maximum execution time in seconds.
        max_memory_mb: Memory cap in megabytes.
    """

    enabled: bool = True
    timeout_seconds: int = Field(default=30, gt=0)
    max_memory_mb: int = Field(default=512, gt=0)


class ExternalSandbox(BaseModel):
    """Configuration for the external (subprocess isolation) sandbox.

    Attributes:
        enabled: Whether the external sandbox is active.
        plugin_dir: Directory path for plugin binaries.
        isolation: Isolation level (``"process"`` or ``"container"``).
        timeout_seconds: Maximum execution time in seconds.
        max_memory_mb: Memory cap in megabytes.
        allowed_hooks: Hook names that external plugins may invoke.
        blocked_hooks: Hook names that are always denied.
        allowed_domains: Network domains external plugins may reach.
        require_signature: Whether plugins must have a verified signature.
        allow_network: Whether outbound network connections are permitted.
        allow_file_write: Whether plugins may write to the filesystem.
        audit_enabled: Whether all sandbox events are logged for audit.
        audit_log_dir: Directory for audit log files.
        audit_retention_days: How many days audit logs are kept.
    """

    enabled: bool = True
    plugin_dir: str = str(_PROJECT_ROOT / "plugins")
    isolation: str = "process"
    timeout_seconds: int = Field(default=SANDBOX_TIMEOUT, gt=0)
    max_memory_mb: int = Field(default=SANDBOX_MAX_MEMORY_MB, gt=0)
    allowed_hooks: list[str] = Field(default_factory=list)
    blocked_hooks: list[str] = Field(default_factory=list)
    allowed_domains: list[str] = Field(default_factory=list)
    require_signature: bool = True
    allow_network: bool = False
    allow_file_write: bool = False
    audit_enabled: bool = True
    audit_log_dir: str = str(_PROJECT_ROOT / "logs" / "sandbox")
    audit_retention_days: int = Field(default=30, ge=1)

    @model_validator(mode="after")
    def _reject_unsigned_sensitive_permissions(self) -> ExternalSandbox:
        """Fail closed for sensitive external plugin capabilities."""
        if self.enabled and (self.allow_network or self.allow_file_write) and not self.require_signature:
            raise ValueError("external sandbox network/file-write permissions require signed plugins")
        if self.enabled and self.allow_network and not self.allowed_domains:
            raise ValueError("external sandbox network access requires an explicit allowed_domains list")
        return self


class SandboxSection(BaseModel):
    """Container for subprocess and external sandbox configs.

    Attributes:
        subprocess: Subprocess (CodeSandbox) execution settings.
        external: External process-isolation sandbox settings.
    """

    subprocess: SubprocessSandbox = Field(default_factory=SubprocessSandbox)
    external: ExternalSandbox = Field(default_factory=ExternalSandbox)


class SandboxRules(BaseModel):
    """High-level permission rules applied across all sandbox modes.

    Attributes:
        allow_code_execution: Whether arbitrary code execution is permitted.
        require_approval_for_external: Whether external calls need user approval.
        allow_network: Whether network access is globally permitted.
        blocked_domains: Domain patterns always denied regardless of other rules.
        allow_file_read: Whether reading the filesystem is permitted.
        allow_file_write: Whether writing to the filesystem is permitted.
        blocked_paths: Filesystem paths that are always off-limits.
    """

    allow_code_execution: bool = False
    require_approval_for_external: bool = True
    allow_network: bool = False
    blocked_domains: list[str] = Field(default_factory=list)
    allow_file_read: bool = True
    allow_file_write: bool = False
    blocked_paths: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _reject_unapproved_code_execution(self) -> SandboxRules:
        """Code execution cannot be enabled without an approval gate."""
        if self.allow_code_execution and not self.require_approval_for_external:
            raise ValueError("code execution requires an external approval gate")
        return self


class ApprovalConfig(BaseModel):
    """Approval gate configuration controlling which actions require confirmation.

    Attributes:
        auto_approve_builtin: Whether built-in operations bypass approval prompts.
        require_approval_for: List of operation names that always need approval.
    """

    auto_approve_builtin: bool = False
    require_approval_for: list[str] = Field(default_factory=list)


class SandboxPolicyConfig(BaseModel):
    """Complete sandbox_policy.yaml configuration.

    Attributes:
        version: Schema version string for compatibility checks.
        sandbox: Sandbox mode settings (in-process and external).
        rules: Global permission rules applied across all sandboxes.
        approval: Approval gate configuration.
    """

    version: str = "1.0"
    sandbox: SandboxSection = Field(default_factory=SandboxSection)
    rules: SandboxRules = Field(default_factory=SandboxRules)
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)

    @classmethod
    def from_yaml_file(cls, path: Path) -> SandboxPolicyConfig:
        """Load and validate sandbox_policy.yaml from the given path.

        Args:
            path: Path to ``sandbox_policy.yaml``.

        Returns:
            Validated :class:`SandboxPolicyConfig` instance.

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError: If the YAML is malformed or fails schema validation.
        """
        return cls.model_validate(_load_yaml(path))
