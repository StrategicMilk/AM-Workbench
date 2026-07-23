"""File jurisdiction enforcement for Vetinari agents.

Validates that a file path falls within the agent's permitted jurisdiction
as defined in ``AgentSpec.jurisdiction``.
"""

from __future__ import annotations

import logging
import os.path

from vetinari.agents.contracts import get_agent_spec
from vetinari.exceptions import JurisdictionViolation
from vetinari.privacy import privacy_receipt
from vetinari.types import AgentType

logger = logging.getLogger(__name__)


class FileJurisdictionEnforcer:
    """Validates file paths against the agent's jurisdiction list in AgentSpec.

    A file is considered within jurisdiction if its normalised path starts with
    any entry in ``AgentSpec.jurisdiction``, matched at a path-component
    boundary (so ``docs/`` cannot be bypassed by ``docs/../vetinari/``).

    Path normalisation:
    - Backslashes replaced with forward slashes (cross-platform).
    - ``..`` path traversal segments resolved via ``os.path.normpath`` so that
      ``docs/../vetinari/agents/contracts.py`` is collapsed to
      ``vetinari/agents/contracts.py`` before prefix matching.

    Example:
        >>> enforcer = FileJurisdictionEnforcer()
        >>> enforcer.validate(AgentType.INSPECTOR, "tests/test_foo.py")  # passes
        >>> enforcer.validate(AgentType.WORKER, "vetinari/core/main.py")  # passes or raises
    """

    @staticmethod
    def _normalise(path: str) -> str:
        """Resolve path traversal and normalise separators for jurisdiction checking.

        Replaces backslashes, resolves ``..`` segments without touching the
        filesystem (safe for untrusted input), then normalises back to forward
        slashes for cross-platform comparison.

        ``os.path.normpath`` is the correct tool here because it collapses
        ``..`` segments in relative paths (``PurePosixPath`` does not).

        Args:
            path: Raw file path from any platform, possibly containing ``..``.

        Returns:
            Normalised path string with forward slashes and no ``..`` segments.
        """
        # Step 1: unified separators before normpath so it works on any platform
        cleaned = path.replace("\\", "/")
        # Step 2: collapse .. segments — normpath handles "a/b/../c" → "a/c"
        resolved = os.path.normpath(cleaned)
        # Step 3: normalise back to forward slashes (normpath uses OS separator)
        return resolved.replace("\\", "/")

    def validate(self, agent_type: AgentType, file_path: str) -> None:
        """Validate that file_path is within the agent's jurisdiction.

        Resolves path traversal before matching so that
        ``docs/../vetinari/secret.py`` cannot bypass a jurisdiction that only
        allows ``docs/``.  Prefix matching uses a path-component boundary
        check (``==`` or ``startswith(entry + "/")``) so that ``vetinari/``
        does not match ``vetinari_evil/``.

        Args:
            agent_type: The agent type whose specification provides the
                jurisdiction list.
            file_path: The file path the agent intends to access or modify.

        Raises:
            JurisdictionViolation: If file_path does not match any entry in
                spec.jurisdiction and the jurisdiction list is non-empty.
        """
        spec = get_agent_spec(agent_type)
        if spec is None:
            raise JurisdictionViolation(
                f"Agent {agent_type.value!r} has no AgentSpec; jurisdiction is unknown.",
                agent_type=agent_type.value,
                file_path=file_path,
                jurisdiction=(),
            )

        jurisdiction = spec.jurisdiction
        # An empty jurisdiction list means no restriction — allow everything.
        if not jurisdiction:
            logger.debug(
                "Jurisdiction check skipped for %s: no jurisdiction constraints defined",
                agent_type.value,
            )
            return

        normalised_file = self._normalise(file_path)

        for entry in jurisdiction:
            normalised_entry = self._normalise(entry)
            # Use exact match OR a slash-terminated prefix so that "docs/"
            # matches "docs/foo.py" but not "docs_evil/foo.py".
            if normalised_file == normalised_entry or normalised_file.startswith(normalised_entry.rstrip("/") + "/"):
                logger.debug(
                    "Jurisdiction check passed for %s: %r matches %r",
                    agent_type.value,
                    file_path,
                    entry,
                )
                return

        raise JurisdictionViolation(
            f"Agent {agent_type.value!r} is not permitted to access {file_path!r}. "
            f"Allowed jurisdiction prefixes: {jurisdiction}. "
            "Update AgentSpec.jurisdiction to include this path or use an authorised agent.",
            agent_type=agent_type.value,
            file_path=file_path,
            jurisdiction=jurisdiction,
        )

    def privacy_receipt_for_path(self, agent_type: AgentType, file_path: str) -> dict[str, object]:
        """Validate access and return an operational privacy receipt.

        Args:
            agent_type: Agent whose jurisdiction is checked before emitting
                the receipt.
            file_path: File path being accessed or modified.

        Returns:
            Operational privacy receipt metadata for the jurisdiction decision.

        Raises:
            JurisdictionViolation: If the agent has no specification or the
                path is outside the agent's jurisdiction.
        """
        self.validate(agent_type, file_path)
        return privacy_receipt(
            privacy_class="operational",
            source=f"jurisdiction:{agent_type.value}",
            retention_days=30,
            redaction_applied=True,
        )
