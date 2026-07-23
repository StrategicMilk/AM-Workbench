"""GitHub Issues adapter for the unified issue tracker interface.

Wraps the GitHub REST API v3 (api.github.com) to implement the
``IssueTracker`` abstract interface. All credentials come from the
constructor — never from environment variables or module-level state.

GitHub API reference: https://docs.github.com/en/rest/issues
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from vetinari.http import create_session
from vetinari.integrations.issue_tracker import (
    CreateIssueRequest,
    Issue,
    IssuePriority,
    IssueStatus,
    IssueTracker,
    IssueTrackerError,
)
from vetinari.security.fail_closed import UntrustedInputError, assert_closed_schema, sanitize_untrusted_text
from vetinari.security.redaction import redact_route_payload, redact_text

logger = logging.getLogger(__name__)


# -- Constants ----------------------------------------------------------------

_GITHUB_API_BASE = "https://api.github.com"
_REQUEST_TIMEOUT = 10  # seconds

# GitHub does not have a native "priority" concept; we encode priority as a
# label using this mapping so existing label-based workflows still work.
_PRIORITY_LABEL: dict[IssuePriority, str] = {
    IssuePriority.CRITICAL: "priority: critical",
    IssuePriority.HIGH: "priority: high",
    IssuePriority.MEDIUM: "priority: medium",
    IssuePriority.LOW: "priority: low",
}

# GitHub state strings -> normalized IssueStatus
_STATE_MAP: dict[str, IssueStatus] = {
    "open": IssueStatus.OPEN,
    "closed": IssueStatus.CLOSED,
}


# -- Helpers ------------------------------------------------------------------


def _parse_priority_from_labels(labels: list[dict[str, Any]]) -> IssuePriority:
    """Derive a normalized priority from a list of GitHub label objects.

    Scans label names for the ``_PRIORITY_LABEL`` mapping values. Falls back
    to MEDIUM when no priority label is found.

    Args:
        labels: List of GitHub label dicts, each with at least a ``name`` key.

    Returns:
        The highest priority found, or IssuePriority.MEDIUM if none matched.
    """
    for label in labels:
        assert_closed_schema(label, allowed_keys={"id", "node_id", "url", "name", "color", "default", "description"})
    label_names = {sanitize_untrusted_text(lbl.get("name", ""), max_length=256).lower() for lbl in labels}
    # Check in descending priority order so we return the highest one present.
    for priority in (IssuePriority.CRITICAL, IssuePriority.HIGH, IssuePriority.LOW):
        if _PRIORITY_LABEL[priority].lower() in label_names:
            return priority
    return IssuePriority.MEDIUM


def _parse_issue(raw: dict[str, Any], owner: str, repo: str) -> Issue:
    """Convert a raw GitHub Issues API response dict into a normalized Issue.

    Args:
        raw: The JSON object returned by the GitHub Issues API.
        owner: Repository owner login; used to reconstruct metadata.
        repo: Repository name; used to reconstruct metadata.

    Returns:
        A populated Issue with all standard fields set.
    """
    normalized_raw = {"state": "open", **raw}
    assert_closed_schema(
        normalized_raw,
        allowed_keys={
            "number",
            "title",
            "body",
            "labels",
            "assignee",
            "state",
            "html_url",
            "pull_request",
            "id",
            "node_id",
            "url",
            "repository_url",
            "comments_url",
            "events_url",
            "labels_url",
            "user",
            "locked",
            "comments",
            "created_at",
            "updated_at",
            "closed_at",
            "author_association",
            "active_lock_reason",
            "draft",
            "performed_via_github_app",
            "state_reason",
        },
        required_keys={"number", "title", "state"},
    )
    raw = normalized_raw
    labels_raw: list[dict[str, Any]] = raw.get("labels", [])
    label_names = [lbl.get("name", "") for lbl in labels_raw if lbl.get("name")]
    priority = _parse_priority_from_labels(labels_raw)
    assignee_data = raw.get("assignee") or {}
    assignee = assignee_data.get("login") if assignee_data else None
    github_state = raw.get("state", "open")
    status = _STATE_MAP.get(github_state, IssueStatus.OPEN)

    return Issue(
        id=str(raw.get("number", "")),
        title=sanitize_untrusted_text(raw.get("title", ""), max_length=512),
        description=redact_text(raw.get("body") or ""),
        priority=priority,
        status=status,
        labels=label_names,
        assignee=assignee,
        url=sanitize_untrusted_text(raw.get("html_url", ""), max_length=2048),
        tracker_type="github",
        raw_data=redact_route_payload(raw),
    )


# -- Adapter ------------------------------------------------------------------


class GitHubIssueTracker(IssueTracker):
    """Issue tracker adapter backed by the GitHub REST API v3.

    Uses Basic auth (token-based) via the ``Authorization: token <PAT>``
    header. All API calls use the shared session from ``vetinari.http`` for
    connection pooling and consistent retry behaviour.

    Args:
        token: GitHub personal access token with ``repo`` scope.
        owner: Repository owner login (user or organization).
        repo: Repository name (without owner prefix).
    """

    def __init__(self, token: str, owner: str, repo: str) -> None:
        self._owner = sanitize_untrusted_text(owner, max_length=128)
        self._repo = sanitize_untrusted_text(repo, max_length=128)
        safe_token = sanitize_untrusted_text(token, max_length=4096)
        self._base = f"{_GITHUB_API_BASE}/repos/{self._owner}/{self._repo}"
        self._session = create_session(
            headers={
                "Authorization": f"token {safe_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    def _url(self, path: str) -> str:
        """Build a full API URL from a relative path segment.

        Args:
            path: Path segment relative to the repo base (e.g. ``"/issues/1"``).

        Returns:
            Absolute GitHub API URL.
        """
        safe_path = sanitize_untrusted_text(path, max_length=512)
        if not safe_path.startswith("/") or ".." in safe_path.split("/"):
            raise UntrustedInputError("GitHub API path must be repo-relative")
        return f"{self._base}{safe_path}"

    def create_issue(self, request: CreateIssueRequest) -> Issue:
        """Create a new GitHub issue.

        Attaches a priority label derived from ``request.priority`` in addition
        to any labels explicitly listed in ``request.labels``.

        Args:
            request: Title, description, priority, labels, and optional assignee.

        Returns:
            The newly created Issue populated from the API response.

        Raises:
            IssueTrackerError: If the API returns a non-2xx status or the
                               network call fails.
        """
        priority_label = _PRIORITY_LABEL[request.priority]
        all_labels = list(
            dict.fromkeys([
                priority_label,
                *(sanitize_untrusted_text(label, max_length=256) for label in request.labels),
            ])
        )

        payload: dict[str, Any] = {
            "title": sanitize_untrusted_text(request.title, max_length=512),
            "body": sanitize_untrusted_text(request.description, max_length=20_000) if request.description else "",
            "labels": all_labels,
        }
        if request.assignee:
            payload["assignees"] = [sanitize_untrusted_text(request.assignee, max_length=128)]

        try:
            resp = self._session.post(
                self._url("/issues"),
                json=payload,
                timeout=_REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise IssueTrackerError(
                f"GitHub API connection failed while creating issue '{request.title}' "
                f"in {self._owner}/{self._repo} — check network connectivity"
            ) from exc

        if not resp.ok:
            raise IssueTrackerError(
                f"GitHub API returned {resp.status_code} when creating issue "
                f"'{request.title}' in {self._owner}/{self._repo}: {resp.text[:200]}",
                status_code=resp.status_code,
            )

        raw: dict[str, Any] = resp.json()
        logger.info(
            "Created GitHub issue #%s in %s/%s",
            raw.get("number"),
            self._owner,
            self._repo,
        )
        return _parse_issue(raw, self._owner, self._repo)

    def get_issue(self, issue_id: str) -> Issue | None:
        """Fetch a single GitHub issue by its number.

        Args:
            issue_id: The GitHub issue number as a string (e.g. ``"42"``).

        Returns:
            The Issue if found, or None if the tracker returns 404.

        Raises:
            IssueTrackerError: On connection failure or non-404 error responses.
        """
        safe_issue_id = sanitize_untrusted_text(issue_id, max_length=32)
        if not safe_issue_id.isdigit():
            raise UntrustedInputError("GitHub issue_id must be numeric")
        try:
            resp = self._session.get(
                self._url(f"/issues/{safe_issue_id}"),
                timeout=_REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise IssueTrackerError(
                f"GitHub API connection failed while fetching issue #{safe_issue_id} "
                f"in {self._owner}/{self._repo} — check network connectivity"
            ) from exc

        if resp.status_code == 404:
            return None
        if not resp.ok:
            raise IssueTrackerError(
                f"GitHub API returned {resp.status_code} when fetching issue #{safe_issue_id} "
                f"in {self._owner}/{self._repo}: {resp.text[:200]}",
                status_code=resp.status_code,
            )

        return _parse_issue(resp.json(), self._owner, self._repo)

    def list_issues(self, status: IssueStatus | None = None, limit: int = 50) -> list[Issue]:
        """List issues in the repository, optionally filtered by status.

        GitHub maps OPEN -> ``state=open`` and CLOSED/RESOLVED -> ``state=closed``.
        IN_PROGRESS has no direct GitHub equivalent; those issues are returned
        under ``state=open``.

        Args:
            status: Filter by normalized status. None returns all states.
            limit: Maximum number of issues to return (capped at 100 per GitHub
                   page limits; use multiple calls for larger sets).

        Returns:
            List of Issue objects sorted newest first.

        Raises:
            IssueTrackerError: On connection failure or API errors.
        """
        if limit < 1:
            raise ValueError("limit must be positive")
        params: dict[str, Any] = {"per_page": min(limit, 100)}

        if status is None:
            params["state"] = "all"
        elif status in (IssueStatus.CLOSED, IssueStatus.RESOLVED):
            params["state"] = "closed"
        else:
            params["state"] = "open"

        try:
            resp = self._session.get(
                self._url("/issues"),
                params=params,
                timeout=_REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise IssueTrackerError(
                f"GitHub API connection failed while listing issues in "
                f"{self._owner}/{self._repo} — check network connectivity"
            ) from exc

        if not resp.ok:
            raise IssueTrackerError(
                f"GitHub API returned {resp.status_code} when listing issues in "
                f"{self._owner}/{self._repo}: {resp.text[:200]}",
                status_code=resp.status_code,
            )

        raw_list: list[dict[str, Any]] = resp.json()
        # GitHub returns both issues and pull requests; filter out PRs.
        issues = [_parse_issue(item, self._owner, self._repo) for item in raw_list if "pull_request" not in item]
        return issues[:limit]

    def update_status(self, issue_id: str, status: IssueStatus) -> bool:
        """Change the open/closed state of a GitHub issue.

        GitHub only supports ``open`` and ``closed`` states. RESOLVED and
        IN_PROGRESS are mapped: RESOLVED -> closed, IN_PROGRESS -> open.

        Args:
            issue_id: GitHub issue number as a string.
            status: The target normalized status.

        Returns:
            True if updated successfully, False if the issue was not found.

        Raises:
            IssueTrackerError: On connection failure or non-404 API errors.
        """
        safe_issue_id = sanitize_untrusted_text(issue_id, max_length=32)
        if not safe_issue_id.isdigit():
            raise UntrustedInputError("GitHub issue_id must be numeric")
        github_state = "closed" if status in (IssueStatus.CLOSED, IssueStatus.RESOLVED) else "open"

        try:
            resp = self._session.patch(
                self._url(f"/issues/{safe_issue_id}"),
                json={"state": github_state},
                timeout=_REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise IssueTrackerError(
                f"GitHub API connection failed while updating issue #{safe_issue_id} "
                f"in {self._owner}/{self._repo} — check network connectivity"
            ) from exc

        if resp.status_code == 404:
            return False
        if not resp.ok:
            raise IssueTrackerError(
                f"GitHub API returned {resp.status_code} when updating issue #{safe_issue_id}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        return True

    def add_comment(self, issue_id: str, comment: str) -> bool:
        """Post a comment on a GitHub issue.

        Args:
            issue_id: GitHub issue number as a string.
            comment: Markdown-formatted comment body.

        Returns:
            True if the comment was created, False if the issue was not found.

        Raises:
            IssueTrackerError: On connection failure or non-404 API errors.
        """
        safe_issue_id = sanitize_untrusted_text(issue_id, max_length=32)
        if not safe_issue_id.isdigit():
            raise UntrustedInputError("GitHub issue_id must be numeric")
        safe_comment = sanitize_untrusted_text(comment, max_length=20_000)
        try:
            resp = self._session.post(
                self._url(f"/issues/{safe_issue_id}/comments"),
                json={"body": safe_comment},
                timeout=_REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise IssueTrackerError(
                f"GitHub API connection failed while adding comment to issue #{safe_issue_id} "
                f"in {self._owner}/{self._repo} — check network connectivity"
            ) from exc

        if resp.status_code == 404:
            return False
        if not resp.ok:
            raise IssueTrackerError(
                f"GitHub API returned {resp.status_code} when adding comment to issue #{safe_issue_id}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        return True
