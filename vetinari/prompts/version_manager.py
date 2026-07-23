"""Vetinari Prompt Version Manager.

Tracks prompt changes with semantic versioning per agent mode.
Stores prompt history in ``vetinari/prompts/versions/`` with timestamps
and SHA-256 checksums. Supports rollback and integration with
PromptEvolver for automatic quality regression rollback.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vetinari.boundary_guards import account_evidence_drop
from vetinari.learning.atomic_writers import write_json_atomic

logger = logging.getLogger(__name__)


_VERSIONS_DIR = Path(__file__).resolve().parent / "versions"


class PromptVersion:
    """A single prompt version entry.

    Attributes:
        version: Semantic version string (e.g. "1.0.0").
        prompt_text: The full prompt text for this version.
        checksum: SHA-256 hex digest of the prompt text.
        timestamp: ISO 8601 UTC timestamp of when this version was saved.
        quality_score: Optional quality score (0.0-1.0) from PromptEvolver.
        notes: Optional human-readable notes about this version.
    """

    def __init__(
        self,
        version: str,
        prompt_text: str,
        checksum: str,
        timestamp: str,
        quality_score: float | None = None,
        notes: str = "",
    ) -> None:
        self.version = version
        self.prompt_text = prompt_text
        self.checksum = checksum
        self.timestamp = timestamp
        self.quality_score = quality_score
        self.notes = notes

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary.

        Returns:
            Dict representation of this version.
        """
        return {
            "version": self.version,
            "prompt_text": self.prompt_text,
            "checksum": self.checksum,
            "timestamp": self.timestamp,
            "quality_score": self.quality_score,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PromptVersion:
        """Deserialize from dictionary.

        Args:
            data: Dict with version data.

        Returns:
            PromptVersion instance.
        """
        return cls(
            version=data["version"],
            prompt_text=data["prompt_text"],
            checksum=data["checksum"],
            timestamp=data["timestamp"],
            quality_score=data.get("quality_score"),
            notes=data.get("notes", ""),
        )


def _compute_checksum(text: str) -> str:
    """Compute SHA-256 checksum of prompt text.

    Args:
        text: The prompt text to checksum.

    Returns:
        Hex digest string.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _version_file_path(agent_type: str, mode: str) -> Path:
    """Get the path to the version history file for an agent-mode pair.

    Args:
        agent_type: Agent type value (e.g. "WORKER").
        mode: Mode name (e.g. "build").

    Returns:
        Path to the JSON version file.
    """
    safe_name = f"{agent_type.lower()}_{mode.lower()}"
    return _VERSIONS_DIR / f"{safe_name}.json"


def _increment_version(current: str) -> str:
    """Increment the patch component of a semantic version.

    Args:
        current: Current version string (e.g. "1.0.2").

    Returns:
        Incremented version string (e.g. "1.0.3").
    """
    parts = current.split(".")
    if len(parts) != 3:
        return "1.0.1"
    try:
        major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
        return f"{major}.{minor}.{patch + 1}"
    except ValueError:
        logger.warning("Version string %r has non-integer parts — resetting to 1.0.1", current)
        return "1.0.1"


class PromptVersionManager:
    """Manages prompt version history with save, rollback, and quality tracking.

    Version histories are stored as JSON files in the versions directory,
    one file per agent-mode combination.
    """

    def __init__(self, versions_dir: Path | None = None) -> None:
        """Resolve the version storage directory, creating it if it does not yet exist.

        Args:
            versions_dir: Override directory for version storage.
                Defaults to ``vetinari/prompts/versions/``.
        """
        self._dir = versions_dir or _VERSIONS_DIR
        self._dir.mkdir(parents=True, exist_ok=True)

    def save_version(
        self,
        agent_type: str,
        mode: str,
        prompt_text: str,
        notes: str = "",
        quality_score: float | None = None,
    ) -> PromptVersion:
        """Save a new prompt version.

        Skips saving if the prompt text is identical to the latest version
        (same SHA-256 checksum).

        Args:
            agent_type: Agent type value (e.g. "WORKER").
            mode: Mode name (e.g. "build").
            prompt_text: The full prompt text to version.
            notes: Optional notes about this version.
            quality_score: Optional quality score from PromptEvolver.

        Returns:
            The saved PromptVersion (or the existing latest if unchanged).

        Raises:
            ValueError: If the existing prompt version history is corrupt.
        """
        checksum = _compute_checksum(prompt_text)
        try:
            history = self._load_history(agent_type, mode)
        except ValueError:
            account_evidence_drop(
                f"{agent_type}:{mode}",
                "prompt_version_history_corrupt",
                logger=logger,
            )
            raise

        # Skip if identical to latest
        if history and history[-1]["checksum"] == checksum:
            logger.debug(
                "Prompt unchanged for %s:%s, skipping version save",
                agent_type,
                mode,
            )
            return PromptVersion.from_dict(history[-1])

        # Determine next version
        if history:
            current_version = history[-1]["version"]
            next_version = _increment_version(current_version)
        else:
            next_version = "1.0.0"

        version = PromptVersion(
            version=next_version,
            prompt_text=prompt_text,
            checksum=checksum,
            timestamp=datetime.now(timezone.utc).isoformat(),
            quality_score=quality_score,
            notes=notes,
        )

        history.append(version.to_dict())
        self._save_history(agent_type, mode, history)
        logger.info(
            "Saved prompt version %s for %s:%s",
            next_version,
            agent_type,
            mode,
        )
        return version

    def get_history(
        self,
        agent_type: str,
        mode: str,
    ) -> list[PromptVersion]:
        """Get the version history for an agent-mode pair.

        Args:
            agent_type: Agent type value.
            mode: Mode name.

        Returns:
            List of PromptVersion objects, oldest first.
        """
        raw = self._load_history(agent_type, mode)
        return [PromptVersion.from_dict(entry) for entry in raw]

    def get_latest(
        self,
        agent_type: str,
        mode: str,
    ) -> PromptVersion | None:
        """Get the latest prompt version.

        Args:
            agent_type: Agent type value.
            mode: Mode name.

        Returns:
            Latest PromptVersion, or None if no history exists.
        """
        history = self.get_history(agent_type, mode)
        return history[-1] if history else None

    def rollback(
        self,
        agent_type: str,
        mode: str,
        version: str,
    ) -> PromptVersion | None:
        """Rollback to a specific version by copying it as a new latest entry.

        Args:
            agent_type: Agent type value.
            mode: Mode name.
            version: The version string to rollback to.

        Returns:
            The new PromptVersion after rollback, or None if version not found.
        """
        history = self._load_history(agent_type, mode)
        target = None
        for entry in history:
            if entry["version"] == version:
                target = entry
                break

        if target is None:
            logger.warning(
                "Version %s not found for %s:%s",
                version,
                agent_type,
                mode,
            )
            return None

        # Save the rolled-back prompt as a new version
        return self.save_version(
            agent_type,
            mode,
            prompt_text=target["prompt_text"],
            notes=f"Rollback from {history[-1]['version']} to {version}",
            quality_score=target.get("quality_score"),
        )

    def auto_rollback_on_regression(
        self,
        agent_type: str,
        mode: str,
        current_score: float,
        threshold: float = 0.1,
    ) -> PromptVersion | None:
        """Auto-rollback if quality regressed beyond threshold.

        Compares current_score with the previous version's quality_score.
        If the regression exceeds threshold, rolls back to the previous version.

        Args:
            agent_type: Agent type value.
            mode: Mode name.
            current_score: The current quality score (0.0-1.0).
            threshold: Maximum acceptable quality drop before rollback.

        Returns:
            The rolled-back PromptVersion if rollback occurred, None otherwise.
        """
        history = self.get_history(agent_type, mode)
        if len(history) < 2:
            return None

        previous = history[-2]
        if previous.quality_score is None:
            return None

        regression = previous.quality_score - current_score
        if regression > threshold:
            logger.warning(
                "Quality regression detected for %s:%s — "
                "previous=%.3f, current=%.3f, regression=%.3f (threshold=%.3f). "
                "Rolling back to %s.",
                agent_type,
                mode,
                previous.quality_score,
                current_score,
                regression,
                threshold,
                previous.version,
            )
            return self.rollback(agent_type, mode, previous.version)

        return None

    def _get_version_file(self, agent_type: str, mode: str) -> Path:
        """Get the path to the version history file using instance directory.

        Args:
            agent_type: Agent type value.
            mode: Mode name.

        Returns:
            Path to the JSON version file.

        Raises:
            ValueError: If the agent_type or mode contains path traversal
                sequences that would place the file outside the versions directory.
        """
        safe_name = f"{agent_type.lower()}_{mode.lower()}"
        target = (self._dir / f"{safe_name}.json").resolve()
        if not target.is_relative_to(self._dir.resolve()):
            raise ValueError(f"Agent type contains path traversal: {agent_type}")
        return target

    def _load_history(self, agent_type: str, mode: str) -> list[dict[str, Any]]:
        """Load version history from disk.

        Args:
            agent_type: Agent type value.
            mode: Mode name.

        Returns:
            List of version dicts.
        """
        filepath = self._get_version_file(agent_type, mode)
        if not filepath.exists():
            return []

        try:
            data = json.loads(filepath.read_text(encoding="utf-8"))
            return data.get("versions", [])
        except json.JSONDecodeError as exc:
            logger.warning("Failed to decode version history: %s", exc)
            account_evidence_drop(str(filepath), "prompt_version_history_corrupt", logger=logger)
            raise ValueError(f"version history is corrupt: {filepath}") from exc
        except OSError as exc:
            logger.warning("Failed to load version history: %s", exc)
            return []

    def _save_history(
        self,
        agent_type: str,
        mode: str,
        history: list[dict[str, Any]],
    ) -> None:
        """Save version history to disk.

        Args:
            agent_type: Agent type value.
            mode: Mode name.
            history: List of version dicts to save.
        """
        filepath = self._get_version_file(agent_type, mode)
        data = {
            "agent_type": agent_type,
            "mode": mode,
            "versions": history,
        }
        write_json_atomic(filepath, data)


# Singleton
_version_manager: PromptVersionManager | None = None
_version_manager_lock = threading.Lock()


def get_version_manager() -> PromptVersionManager:
    """Get the singleton PromptVersionManager instance.

    Returns:
        The PromptVersionManager singleton.
    """
    global _version_manager
    if _version_manager is None:
        with _version_manager_lock:
            if _version_manager is None:
                _version_manager = PromptVersionManager()
    return _version_manager
