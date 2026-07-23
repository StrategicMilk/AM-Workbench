"""Policy loading and configuration access for the autonomy governor."""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from vetinari.autonomy.governor_levels import _mode_default
from vetinari.autonomy.governor_models import ActionPolicy
from vetinari.types import AutonomyLevel, AutonomyMode, DomainCareLevel

logger = logging.getLogger(__name__)

_DEFAULT_POLICY_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "autonomy_policies.yaml"


class _GovernorPolicyMixin:
    """Policy loading, defaulting, and autonomy-mode accessors."""

    if TYPE_CHECKING:
        _autonomy_mode: Any
        _default_level: Any
        _domain_care_levels: Any
        _lock: Any
        _policies: Any
        _policy_path: Any

    def _load_policies(self) -> None:
        """Load action policies from the YAML configuration file."""
        if not self._policy_path.exists():
            logger.warning(
                "Autonomy policy file not found at %s; using default L1 for all actions",
                self._policy_path,
            )
            return

        try:
            raw = self._policy_path.read_text(encoding="utf-8")
            data = yaml.safe_load(raw)
        except (OSError, yaml.YAMLError):
            logger.warning(
                "Failed to read autonomy policies from %s; using default policies until the file is fixed",
                self._policy_path,
                exc_info=True,
            )
            return

        if not isinstance(data, dict):
            logger.warning("Autonomy policy file has invalid structure; expected top-level dict")
            return

        try:
            self._load_action_policies(data.get("actions", {}))
            self._load_default_policy(data.get("defaults", {}))
            self._load_autonomy_mode(data.get("global_autonomy_mode"))
            self._load_domain_care_levels(data.get("domain_care_levels", {}))
        except (AttributeError, TypeError, ValueError):
            logger.warning(
                "Failed to parse autonomy policies from %s; using policies parsed before the error and defaults for the rest",
                self._policy_path,
                exc_info=True,
            )

    def _load_action_policies(self, actions: Any) -> None:
        """Load per-action policy entries from parsed YAML data."""
        for action_type, config in actions.items():
            if not isinstance(config, dict):
                logger.warning("Skipping invalid policy entry for %s; expected dict", action_type)
                continue
            level_str = config.get("level", "L1")
            try:
                level = AutonomyLevel(level_str)
            except ValueError:
                logger.warning(
                    "Unknown autonomy level %r for action %s; defaulting to L1",
                    level_str,
                    action_type,
                )
                level = AutonomyLevel.L1_SUGGEST

            self._policies[action_type] = ActionPolicy(
                level=level,
                max_change_pct=float(config.get("max_change_pct", 100.0)),
                rollback_on_regression=bool(config.get("rollback_on_regression", False)),
            )

        logger.info("Loaded autonomy policies for %d action types", len(self._policies))

    def _load_default_policy(self, defaults: Any) -> None:
        """Load the global default policy level from parsed YAML data."""
        default_level_str = defaults.get("level")
        if default_level_str:
            with contextlib.suppress(ValueError):
                self._default_level = AutonomyLevel(default_level_str)

    def _load_autonomy_mode(self, mode_str: Any) -> None:
        """Load the global autonomy mode from parsed YAML data."""
        if not mode_str:
            return
        try:
            self._autonomy_mode = AutonomyMode(mode_str)
        except ValueError:
            logger.warning(
                "Unknown autonomy mode %r in policy file; defaulting to BALANCED",
                mode_str,
            )

    def _load_domain_care_levels(self, domain_levels: Any) -> None:
        """Load per-domain care levels from parsed YAML data."""
        for domain, care_str in domain_levels.items():
            try:
                self._domain_care_levels[domain] = DomainCareLevel(care_str)
            except ValueError:
                logger.warning(
                    "Unknown domain care level %r for domain %s; skipping",
                    care_str,
                    domain,
                )

    def get_policy(self, action_type: str) -> ActionPolicy:
        """Get the policy for an action type, falling back to default level.

        Args:
            action_type: The action type identifier.

        Returns:
            ActionPolicy for this action type.
        """
        return self._policies.get(action_type, ActionPolicy(level=self._default_level))

    def get_autonomy_mode(self) -> AutonomyMode:
        """Return the current global autonomy mode.

        Returns:
            The active autonomy mode.
        """
        with self._lock:
            return self._autonomy_mode

    def set_autonomy_mode(self, mode: AutonomyMode) -> None:
        """Set the global autonomy mode.

        Args:
            mode: The new autonomy mode to apply.
        """
        with self._lock:
            self._autonomy_mode = mode
        logger.info("Autonomy mode set to %s", mode.value)

    def get_mode_default(self, risk_tier: str) -> AutonomyLevel:
        """Return the default autonomy level for a risk tier under the active mode.

        Args:
            risk_tier: One of ``"risky"``, ``"medium"``, or ``"safe"``.

        Returns:
            AutonomyLevel for the tier, or L1 for unknown tiers.
        """
        with self._lock:
            return _mode_default(self._autonomy_mode, risk_tier)

    def get_domain_care_level(self, domain: str) -> DomainCareLevel | None:
        """Return the care level for a domain, or None if not configured.

        Args:
            domain: Domain identifier.

        Returns:
            DomainCareLevel if configured, None otherwise.
        """
        return self._domain_care_levels.get(domain)
