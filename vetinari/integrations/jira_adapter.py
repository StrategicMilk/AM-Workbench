"""Jira issue tracker adapter using the Jira REST API v3.

Wraps the Jira Cloud REST API v3 (``<url>/rest/api/3/``) to implement the
``IssueTracker`` abstract interface. Authenticates with HTTP Basic auth
using an email address and an API token (not a password).

Jira REST API reference: https://developer.atlassian.com/cloud/jira/platform/rest/v3/
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

logger = logging.getLogger(__name__)


# -- Constants ----------------------------------------------------------------

_REQUEST_TIMEOUT = 10  # seconds

# Jira priority names -> normalized IssuePriority
_JIRA_PRIORITY_MAP: dict[str, IssuePriority] = {
    "blocker": IssuePriority.CRITICAL,
    "critical": IssuePriority.CRITICAL,
    "highest": IssuePriority.CRITICAL,
    "high": IssuePriority.HIGH,
    "medium": IssuePriority.MEDIUM,
    "low": IssuePriority.LOW,
    "lowest": IssuePriority.LOW,
    "trivial": IssuePriority.LOW,
    "minor": IssuePriority.LOW,
}

# Normalized IssuePriority -> Jira priority name
_PRIORITY_TO_JIRA: dict[IssuePriority, str] = {
    IssuePriority.CRITICAL: "Highest",
    IssuePriority.HIGH: "High",
    IssuePriority.MEDIUM: "Medium",
    IssuePriority.LOW: "Low",
}

# Jira status category keys -> normalized IssueStatus
_STATUS_CATEGORY_MAP: dict[str, IssueStatus] = {
    "new": IssueStatus.OPEN,
    "indeterminate": IssueStatus.IN_PROGRESS,
    "done": IssueStatus.RESOLVED,
}


# -- Helpers ------------------------------------------------------------------


def _parse_priority(fields: dict[str, Any]) -> IssuePriority:
    """Extract normalized priority from a Jira issue fields dict.

    Args:
        fields: The ``fields`` sub-dict from a Jira issue JSON object.

    Returns:
        Normalized IssuePriority, defaulting to MEDIUM when unknown.
    """
    priority_data = fields.get("priority") or {}
    name = priority_data.get("name", "").lower()
    return _JIRA_PRIORITY_MAP.get(name, IssuePriority.MEDIUM)


def _parse_status(fields: dict[str, Any]) -> IssueStatus:
    """Extract normalized status from a Jira issue fields dict.

    Uses the status category key (``new``, ``indeterminate``, ``done``) for
    a stable mapping that doesn't break when teams rename their statuses.

    Args:
        fields: The ``fields`` sub-dict from a Jira issue JSON object.

    Returns:
        Normalized IssueStatus, defaulting to OPEN when unknown.
    """
    status_data = fields.get("status") or {}
    category_data = status_data.get("statusCategory") or {}
    category_key = category_data.get("key", "").lower()
    return _STATUS_CATEGORY_MAP.get(category_key, IssueStatus.OPEN)


def _parse_issue(raw: dict[str, Any]) -> Issue:
    """Convert a raw Jira REST API issue object into a normalized Issue.

    Args:
        raw: The JSON object returned by the Jira Issues API.

    Returns:
        A populated Issue with all standard fields set.
    """
    fields: dict[str, Any] = raw.get("fields") or {}
    priority = _parse_priority(fields)
    status = _parse_status(fields)
    assignee_data = fields.get("assignee") or {}
    assignee = assignee_data.get("emailAddress") or assignee_data.get("displayName") or None
    label_list: list[str] = fields.get("labels") or []
    # Jira self-link for web UI
    base_url = ""
    self_link = raw.get("self", "")
    if self_link:
        # Derive the web URL from the REST self-link: replace /rest/api/... with /browse/KEY
        parts = self_link.split("/rest/api/")
        base_url = parts[0] if parts else ""
    issue_key = raw.get("key", "")
    web_url = f"{base_url}/browse/{issue_key}" if base_url and issue_key else ""

    return Issue(
        id=issue_key,
        title=fields.get("summary", ""),
        description=_extract_description(fields),
        priority=priority,
        status=status,
        labels=label_list,
        assignee=assignee,
        url=web_url,
        tracker_type="jira",
        raw_data=raw,
    )


def _extract_description(fields: dict[str, Any]) -> str:
    """Extract plain-text description from Jira's Atlassian Document Format.

    Jira API v3 returns description as ADF (a nested JSON structure).
    This function extracts the text content recursively for display.

    Args:
        fields: The ``fields`` sub-dict from a Jira issue JSON object.

    Returns:
        Plain-text representation of the description, or empty string.
    """
    desc = fields.get("description")
    if desc is None:
        return ""
    if isinstance(desc, str):
        return desc
    # ADF format: walk the content tree collecting text nodes
    return _extract_adf_text(desc)


def _extract_adf_text(node: dict[str, Any]) -> str:
    """Recursively extract text from an Atlassian Document Format node.

    Args:
        node: A single ADF node dict with optional ``text`` and ``content`` keys.

    Returns:
        Concatenated text content of the node and all its descendants.
    """
    parts: list[str] = []
    if node.get("type") == "text":
        parts.append(node.get("text", ""))
    parts.extend(_extract_adf_text(child) for child in node.get("content", []))
    return " ".join(p for p in parts if p)


# -- Adapter ------------------------------------------------------------------


class JiraIssueTracker(IssueTracker):
    """Issue tracker adapter backed by the Jira Cloud REST API v3.

    Uses HTTP Basic auth with an email address and API token. All API calls
    use the shared session from ``vetinari.http`` for connection pooling.

    Args:
        url: Base URL of the Jira instance (e.g. ``https://company.atlassian.net``).
        email: Jira account email address for authentication.
        api_token: Jira API token (not a password — generate at id.atlassian.com).
        project_key: Jira project key (e.g. ``"VET"``). New issues are created
                     here, and list queries are scoped to this project.
    """

    def __init__(self, url: str, email: str, api_token: str, project_key: str) -> None:
        self._base = url.rstrip("/") + "/rest/api/3"
        self._project_key = project_key
        self._session = create_session(headers={"Accept": "application/json", "Content-Type": "application/json"})
        self._session.auth = (email, api_token)

    def _url(self, path: str) -> str:
        """Build a full Jira API URL from a relative path.

        Args:
            path: Path segment relative to the API base (e.g. ``"/issue/VET-1"``).

        Returns:
            Absolute Jira REST API URL.
        """
        return f"{self._base}{path}"

    def create_issue(self, request: CreateIssueRequest) -> Issue:
        """Create a new Jira issue in the configured project.

        The issue type defaults to ``Task``. Description is submitted as
        Atlassian Document Format plain text.

        Args:
            request: Title, description, priority, labels, and optional assignee.

        Returns:
            The newly created Issue fetched back from Jira to get all fields.

        Raises:
            IssueTrackerError: If the API call fails.
        """
        jira_priority = _PRIORITY_TO_JIRA[request.priority]
        # ADF plain-text document for description
        adf_description: dict[str, Any] = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": request.description}],
                }
            ],
        }
        payload: dict[str, Any] = {
            "fields": {
                "project": {"key": self._project_key},
                "summary": request.title,
                "description": adf_description,
                "issuetype": {"name": "Task"},
                "priority": {"name": jira_priority},
                "labels": request.labels,
            }
        }
        if request.assignee:
            payload["fields"]["assignee"] = {"accountId": request.assignee}

        try:
            resp = self._session.post(
                self._url("/issue"),
                json=payload,
                timeout=_REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise IssueTrackerError(
                f"Jira API connection failed while creating issue '{request.title}' "
                f"in project {self._project_key} — check network connectivity"
            ) from exc

        if not resp.ok:
            raise IssueTrackerError(
                f"Jira API returned {resp.status_code} when creating issue '{request.title}': {resp.text[:200]}",
                status_code=resp.status_code,
            )

        created = resp.json()
        issue_key = created.get("key", "")
        logger.info("Created Jira issue %s in project %s", issue_key, self._project_key)

        # Fetch the full issue to populate all normalized fields
        fetched = self.get_issue(issue_key)
        if fetched is None:
            # Construct a minimal Issue from the creation response
            return Issue(
                id=issue_key,
                title=request.title,
                description=request.description,
                priority=request.priority,
                status=IssueStatus.OPEN,
                labels=request.labels,
                tracker_type="jira",
                raw_data=created,
            )
        return fetched

    def get_issue(self, issue_id: str) -> Issue | None:
        """Fetch a single Jira issue by its key or numeric ID.

        Args:
            issue_id: The Jira issue key (e.g. ``"VET-42"``) or numeric ID.

        Returns:
            The Issue if found, or None if Jira returns 404.

        Raises:
            IssueTrackerError: On connection failure or non-404 errors.
        """
        try:
            resp = self._session.get(
                self._url(f"/issue/{issue_id}"),
                timeout=_REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise IssueTrackerError(
                f"Jira API connection failed while fetching issue {issue_id} — check network connectivity"
            ) from exc

        if resp.status_code == 404:
            return None
        if not resp.ok:
            raise IssueTrackerError(
                f"Jira API returned {resp.status_code} when fetching issue {issue_id}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        return _parse_issue(resp.json())

    def list_issues(self, status: IssueStatus | None = None, limit: int = 50) -> list[Issue]:
        """List issues in the configured project using Jira JQL.

        Args:
            status: Filter by normalized status. None returns all states.
            limit: Maximum number of issues to return (Jira caps at 100 per page).

        Returns:
            List of Issue objects sorted by most-recently-updated.

        Raises:
            IssueTrackerError: On connection failure or API errors.
        """
        jql_parts = [f"project = {self._project_key}"]

        if status == IssueStatus.OPEN:
            jql_parts.append('statusCategory = "To Do"')
        elif status == IssueStatus.IN_PROGRESS:
            jql_parts.append('statusCategory = "In Progress"')
        elif status in (IssueStatus.RESOLVED, IssueStatus.CLOSED):
            jql_parts.append("statusCategory = Done")

        jql = " AND ".join(jql_parts) + " ORDER BY updated DESC"

        try:
            resp = self._session.get(
                self._url("/search"),
                params={"jql": jql, "maxResults": min(limit, 100)},
                timeout=_REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise IssueTrackerError(
                f"Jira API connection failed while listing issues in project "
                f"{self._project_key} — check network connectivity"
            ) from exc

        if not resp.ok:
            raise IssueTrackerError(
                f"Jira API returned {resp.status_code} when listing issues: {resp.text[:200]}",
                status_code=resp.status_code,
            )

        body: dict[str, Any] = resp.json()
        issues_raw: list[dict[str, Any]] = body.get("issues", [])
        return [_parse_issue(item) for item in issues_raw[:limit]]

    def update_status(self, issue_id: str, status: IssueStatus) -> bool:
        """Transition a Jira issue to a new status via a workflow transition.

        Jira transitions are workflow-specific. This method queries available
        transitions and picks the first one whose ``to.statusCategory.key``
        matches the target status.

        Args:
            issue_id: Jira issue key (e.g. ``"VET-42"``).
            status: Target normalized status.

        Returns:
            True if transitioned successfully, False if the issue was not found.

        Raises:
            IssueTrackerError: On connection failure or when no matching
                               transition exists.
        """
        try:
            trans_resp = self._session.get(
                self._url(f"/issue/{issue_id}/transitions"),
                timeout=_REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise IssueTrackerError(f"Jira API connection failed while fetching transitions for {issue_id}") from exc

        if trans_resp.status_code == 404:
            return False
        if not trans_resp.ok:
            raise IssueTrackerError(
                f"Jira API returned {trans_resp.status_code} fetching transitions for "
                f"{issue_id}: {trans_resp.text[:200]}",
                status_code=trans_resp.status_code,
            )

        target_category = {
            IssueStatus.OPEN: "new",
            IssueStatus.IN_PROGRESS: "indeterminate",
            IssueStatus.RESOLVED: "done",
            IssueStatus.CLOSED: "done",
        }[status]

        transitions: list[dict[str, Any]] = trans_resp.json().get("transitions", [])
        transition_id = None
        for trans in transitions:
            to_state = trans.get("to") or {}
            cat_key = (to_state.get("statusCategory") or {}).get("key", "").lower()
            if cat_key == target_category:
                transition_id = trans.get("id")
                break

        if not transition_id:
            raise IssueTrackerError(
                f"No Jira transition to status category '{target_category}' found for "
                f"issue {issue_id} — check the project workflow configuration"
            )

        try:
            do_resp = self._session.post(
                self._url(f"/issue/{issue_id}/transitions"),
                json={"transition": {"id": transition_id}},
                timeout=_REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise IssueTrackerError(f"Jira API connection failed while transitioning {issue_id}") from exc

        if do_resp.status_code == 404:
            return False
        if not do_resp.ok:
            raise IssueTrackerError(
                f"Jira API returned {do_resp.status_code} when transitioning {issue_id}: {do_resp.text[:200]}",
                status_code=do_resp.status_code,
            )
        return True

    def add_comment(self, issue_id: str, comment: str) -> bool:
        """Add a comment to a Jira issue.

        Comment body is submitted as Atlassian Document Format plain text.

        Args:
            issue_id: Jira issue key (e.g. ``"VET-42"``).
            comment: Comment text. Plain text only; ADF encoding is applied automatically.

        Returns:
            True if the comment was created, False if the issue was not found.

        Raises:
            IssueTrackerError: On connection failure or non-404 API errors.
        """
        adf_body: dict[str, Any] = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": comment}],
                }
            ],
        }
        try:
            resp = self._session.post(
                self._url(f"/issue/{issue_id}/comment"),
                json={"body": adf_body},
                timeout=_REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise IssueTrackerError(f"Jira API connection failed while adding comment to {issue_id}") from exc

        if resp.status_code == 404:
            return False
        if not resp.ok:
            raise IssueTrackerError(
                f"Jira API returned {resp.status_code} when adding comment to {issue_id}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        return True
