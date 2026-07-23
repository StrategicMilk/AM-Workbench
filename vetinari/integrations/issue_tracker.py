"""Issue tracker integration — abstract interface for external issue systems.

Provides a unified API for creating, updating, and querying issues across
GitHub Issues, Linear, and Jira. Adapters are instantiated via the
``create_issue_tracker`` factory, using the shape shown in
``config/integrations.yaml.example``.

This is a standalone integration layer — it is not part of the core agent
pipeline. It allows Vetinari tasks and quality gates to file issues in an
external tracker without coupling to any specific vendor API.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# -- Enumerations -------------------------------------------------------------

# Timeout applied to every outbound HTTP call. Long enough for slow Jira
# cloud instances; short enough to avoid blocking agent execution indefinitely.
DEFAULT_REQUEST_TIMEOUT = 10  # seconds


class IssuePriority(str, Enum):
    """Normalized priority levels that map to vendor-specific values in each adapter."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class IssueStatus(str, Enum):
    """Normalized issue lifecycle states mapped from vendor-specific state names."""

    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"


# -- Data structures ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Issue:
    """Normalized issue representation across all tracker back-ends.

    Fields map to the lowest common denominator across GitHub, Linear, and
    Jira. Vendor-specific data is preserved in ``raw_data`` for callers that
    need it, but the primary fields are always populated.

    Attributes:
        id: Tracker-assigned identifier (e.g. ``"123"``, ``"VET-42"``).
        title: Short, human-readable summary line.
        description: Full body text, Markdown where supported.
        priority: Normalized priority bucket.
        status: Current lifecycle state.
        labels: Flat list of tag/label strings.
        assignee: Login or email of the assigned user, or None.
        url: Direct link to the issue in the tracker web UI.
        tracker_type: Discriminator string (``"github"``, ``"linear"``, ``"jira"``).
        raw_data: Unmodified payload from the tracker API.
    """

    id: str
    title: str
    description: str = ""
    priority: IssuePriority = IssuePriority.MEDIUM
    status: IssueStatus = IssueStatus.OPEN
    labels: list[str] = field(default_factory=list)
    assignee: str | None = None
    url: str = ""
    tracker_type: str = ""
    raw_data: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        """Return a compact representation showing the issue ID and title."""
        return f"Issue(id={self.id!r}, title={self.title!r}, status={self.status.value})"


@dataclass
class CreateIssueRequest:
    """Input parameters for creating a new issue.

    Attributes:
        title: Short summary line for the issue.
        description: Detailed body text; Markdown is supported by all adapters.
        priority: Desired urgency level; defaults to MEDIUM.
        labels: Tag strings to attach (adapter maps them to tracker labels).
        assignee: Login or email of the user to assign, or None to leave unassigned.
    """

    title: str
    description: str = ""
    priority: IssuePriority = IssuePriority.MEDIUM
    labels: list[str] = field(default_factory=list)
    assignee: str | None = None

    def __repr__(self) -> str:
        return "CreateIssueRequest(...)"


# -- Abstract interface -------------------------------------------------------


class IssueTracker(ABC):
    """Abstract interface for issue tracker integrations.

    Each concrete subclass wraps a specific vendor API (GitHub Issues,
    Linear GraphQL, or Jira REST v3) while exposing a uniform surface so
    that callers never need to know which tracker is configured.

    All methods must be thread-safe because runtime workers and background
    dispatchers may call them concurrently.
    """

    @abstractmethod
    def create_issue(self, request: CreateIssueRequest) -> Issue:
        """Create a new issue in the tracker.

        Args:
            request: Title, description, priority, labels, and optional assignee.

        Returns:
            The newly created issue with all fields populated from the API response.

        Raises:
            IssueTrackerError: If the API call fails or returns an error status.
        """

    @abstractmethod
    def get_issue(self, issue_id: str) -> Issue | None:
        """Fetch a single issue by its tracker-assigned ID.

        Args:
            issue_id: The tracker-native identifier (e.g. ``"123"`` or ``"VET-42"``).

        Returns:
            The populated Issue, or None if no issue with that ID exists.

        Raises:
            IssueTrackerError: If the API call fails for a reason other than 404.
        """

    @abstractmethod
    def list_issues(self, status: IssueStatus | None = None, limit: int = 50) -> list[Issue]:
        """Return a paginated list of issues, optionally filtered by status.

        Args:
            status: When provided, only issues in this state are returned.
                    Pass None to return issues in all states.
            limit: Maximum number of issues to return. Capped at the tracker's
                   own page-size limit if lower.

        Returns:
            List of Issue objects, newest first where the API supports ordering.

        Raises:
            IssueTrackerError: If the API call fails.
        """

    @abstractmethod
    def update_status(self, issue_id: str, status: IssueStatus) -> bool:
        """Transition an issue to a new lifecycle state.

        Args:
            issue_id: Tracker-native identifier of the issue to update.
            status: The target state to move the issue into.

        Returns:
            True when the update was accepted, False when the issue was not found.

        Raises:
            IssueTrackerError: If the API call fails for a reason other than 404.
        """

    @abstractmethod
    def add_comment(self, issue_id: str, comment: str) -> bool:
        """Append a comment to an existing issue.

        Args:
            issue_id: Tracker-native identifier of the issue to comment on.
            comment: The comment body. Markdown is supported by all adapters.

        Returns:
            True when the comment was created, False when the issue was not found.

        Raises:
            IssueTrackerError: If the API call fails for a reason other than 404.
        """


# -- Factory ------------------------------------------------------------------


def create_issue_tracker(config: dict[str, Any]) -> IssueTracker | None:
    """Instantiate an issue tracker from a configuration dictionary.

    Reads the ``issue_tracker`` key from the provided config dict and
    constructs the appropriate adapter. Returns None (with a warning log)
    if the configuration is absent, incomplete, or specifies an unknown type.

    The ``config`` dict is typically loaded from an operator-owned integrations
    file shaped like ``config/integrations.yaml.example`` by the caller; this
    function does not perform file I/O itself.

    Args:
        config: Dictionary containing an ``issue_tracker`` sub-key with at
                minimum a ``type`` field (``"github"``, ``"linear"``, or
                ``"jira"``) and the corresponding credentials block.

    Returns:
        A ready-to-use IssueTracker instance, or None if configuration is
        missing or invalid.

    Example::

        import yaml
        from pathlib import Path
        from vetinari.integrations.issue_tracker import create_issue_tracker

        cfg = yaml.safe_load(Path("config/integrations.yaml.example").read_text(encoding="utf-8"))
        tracker = create_issue_tracker(cfg)
        if tracker:
            issue = tracker.create_issue(CreateIssueRequest(title="Bug found"))
    """
    # Deferred imports avoid circular deps and keep the module importable when
    # the concrete adapters are not installed.
    from vetinari.integrations.github_issues import GitHubIssueTracker
    from vetinari.integrations.jira_adapter import JiraIssueTracker
    from vetinari.integrations.linear_adapter import LinearIssueTracker

    tracker_cfg = config.get("issue_tracker")
    if not tracker_cfg:
        logger.warning("No 'issue_tracker' key found in integration config — issue tracker disabled")
        return None

    tracker_type = tracker_cfg.get("type", "").lower()

    if tracker_type == "github":
        gh = tracker_cfg.get("github", {})
        token = _resolve_secret_value(gh.get("token", ""), field_name="github.token")
        owner = gh.get("owner", "")
        repo = gh.get("repo", "")
        if not (token and owner and repo):
            logger.warning("GitHub issue tracker config is incomplete (need token, owner, repo) — disabled")
            return None
        return GitHubIssueTracker(token=token, owner=owner, repo=repo)

    if tracker_type == "linear":
        lin = tracker_cfg.get("linear", {})
        api_key = _resolve_secret_value(lin.get("api_key", ""), field_name="linear.api_key")
        team_id = lin.get("team_id", "")
        if not (api_key and team_id):
            logger.warning("Linear issue tracker config is incomplete (need api_key, team_id) — disabled")
            return None
        return LinearIssueTracker(api_key=api_key, team_id=team_id)

    if tracker_type == "jira":
        jira = tracker_cfg.get("jira", {})
        url = jira.get("url", "")
        email = jira.get("email", "")
        api_token = _resolve_secret_value(jira.get("api_token", ""), field_name="jira.api_token")
        project_key = jira.get("project_key", "")
        if not (url and email and api_token and project_key):
            logger.warning(
                "Jira issue tracker config is incomplete (need url, email, api_token, project_key) — disabled"
            )
            return None
        return JiraIssueTracker(url=url, email=email, api_token=api_token, project_key=project_key)

    logger.warning(
        "Unknown issue tracker type %r — supported values: github, linear, jira",
        tracker_type,
    )
    return None


def _resolve_secret_value(value: object, *, field_name: str) -> str:
    """Resolve env-backed secret config and reject file-path secret references."""
    if not isinstance(value, str):
        return ""
    raw = value.strip()
    if not raw:
        return ""
    if raw.startswith("${") and raw.endswith("}"):
        raw = f"env:{raw[2:-1]}"
    if raw.lower().startswith("env:"):
        env_name = raw[4:].strip()
        if not env_name:
            raise IssueTrackerError(f"{field_name} env reference is empty")
        return os.environ.get(env_name, "").strip()
    if _looks_like_secret_path(raw):
        raise IssueTrackerError(f"{field_name} must come from an environment variable, not a repository file path")
    if raw in {"<token>", "changeme", "replace-me", "TODO"}:
        return ""
    return raw


def _looks_like_secret_path(value: str) -> bool:
    path = Path(value)
    if path.suffix.lower() in {".yaml", ".yml", ".json", ".toml", ".env", ".txt"}:
        return True
    return "\\" in value or "/" in value


# -- Exception ----------------------------------------------------------------


class IssueTrackerError(Exception):
    """Raised when a tracker API call fails.

    Attributes:
        status_code: HTTP status code returned by the vendor API, or None
                     for connection-level failures.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)
