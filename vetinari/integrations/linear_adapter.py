"""Linear issue tracker adapter using the Linear GraphQL API.

Wraps the Linear GraphQL API (api.linear.app/graphql) to implement the
``IssueTracker`` abstract interface. All credentials are passed via the
constructor — never from environment variables.

Linear API reference: https://developers.linear.app/docs/graphql/working-with-the-graphql-api
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

_LINEAR_API_URL = "https://api.linear.app/graphql"
_REQUEST_TIMEOUT = 10  # seconds

# Linear priority is numeric: 0=No priority, 1=Urgent, 2=High, 3=Medium, 4=Low
_PRIORITY_TO_LINEAR: dict[IssuePriority, int] = {
    IssuePriority.CRITICAL: 1,  # Urgent
    IssuePriority.HIGH: 2,
    IssuePriority.MEDIUM: 3,
    IssuePriority.LOW: 4,
}

_LINEAR_TO_PRIORITY: dict[int, IssuePriority] = {
    1: IssuePriority.CRITICAL,
    2: IssuePriority.HIGH,
    3: IssuePriority.MEDIUM,
    4: IssuePriority.LOW,
}

# Linear state names -> normalized IssueStatus (case-insensitive lookup)
_STATE_NAME_MAP: dict[str, IssueStatus] = {
    "backlog": IssueStatus.OPEN,
    "todo": IssueStatus.OPEN,
    "in progress": IssueStatus.IN_PROGRESS,
    "in review": IssueStatus.IN_PROGRESS,
    "done": IssueStatus.RESOLVED,
    "cancelled": IssueStatus.CLOSED,
    "canceled": IssueStatus.CLOSED,
}

# Linear state type strings -> normalized IssueStatus (fallback mapping)
_STATE_TYPE_MAP: dict[str, IssueStatus] = {
    "backlog": IssueStatus.OPEN,
    "unstarted": IssueStatus.OPEN,
    "started": IssueStatus.IN_PROGRESS,
    "completed": IssueStatus.RESOLVED,
    "cancelled": IssueStatus.CLOSED,
}


# -- Helpers ------------------------------------------------------------------


def _parse_state(state: dict[str, Any]) -> IssueStatus:
    """Convert a Linear state object to a normalized IssueStatus.

    Tries name-based lookup first, then falls back to the state type field.

    Args:
        state: Linear state dict with at least ``name`` and ``type`` keys.

    Returns:
        The best matching IssueStatus, defaulting to OPEN when unknown.
    """
    name = state.get("name", "").lower()
    if name in _STATE_NAME_MAP:
        return _STATE_NAME_MAP[name]
    state_type = state.get("type", "").lower()
    return _STATE_TYPE_MAP.get(state_type, IssueStatus.OPEN)


def _parse_issue(raw: dict[str, Any]) -> Issue:
    """Convert a raw Linear GraphQL issue node into a normalized Issue.

    Args:
        raw: The issue node dict from the Linear GraphQL response.

    Returns:
        A populated Issue with all standard fields set.
    """
    state_data: dict[str, Any] = raw.get("state") or {}
    status = _parse_state(state_data)
    priority_num = raw.get("priority", 3)
    priority = _LINEAR_TO_PRIORITY.get(priority_num, IssuePriority.MEDIUM)
    assignee_data: dict[str, Any] = raw.get("assignee") or {}
    assignee = assignee_data.get("email") or assignee_data.get("name") or None
    label_nodes: list[dict[str, Any]] = (raw.get("labels") or {}).get("nodes", [])
    labels = [lbl.get("name", "") for lbl in label_nodes if lbl.get("name")]

    return Issue(
        id=raw.get("id", ""),
        title=raw.get("title", ""),
        description=raw.get("description") or "",
        priority=priority,
        status=status,
        labels=labels,
        assignee=assignee,
        url=raw.get("url", ""),
        tracker_type="linear",
        raw_data=raw,
    )


def _gql(session: requests.Session, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    """Execute a GraphQL query against the Linear API.

    Args:
        session: Configured requests Session with auth headers.
        query: GraphQL query or mutation string.
        variables: Variables dict to pass alongside the query.

    Returns:
        The ``data`` field of the GraphQL response.

    Raises:
        IssueTrackerError: On connection failure, HTTP error, or GraphQL errors.
    """
    try:
        resp = session.post(
            _LINEAR_API_URL,
            json={"query": query, "variables": variables},
            timeout=_REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise IssueTrackerError("Linear API connection failed — check network connectivity and API key") from exc

    if not resp.ok:
        raise IssueTrackerError(
            f"Linear API returned HTTP {resp.status_code}: {resp.text[:200]}",
            status_code=resp.status_code,
        )

    body: dict[str, Any] = resp.json()
    gql_errors = body.get("errors")
    if gql_errors:
        messages = "; ".join(e.get("message", "unknown") for e in gql_errors)
        raise IssueTrackerError(f"Linear GraphQL errors: {messages}")

    data = body.get("data")
    if data is None:
        raise IssueTrackerError("Linear API response contained no 'data' field")
    return data


# -- Adapter ------------------------------------------------------------------


class LinearIssueTracker(IssueTracker):
    """Issue tracker adapter backed by the Linear GraphQL API.

    Uses the ``Authorization: <api_key>`` header. All API calls use the
    shared session from ``vetinari.http`` for connection pooling.

    Args:
        api_key: Linear personal API key.
        team_id: Linear team ID (UUID string) — issues are created in this team.
    """

    def __init__(self, api_key: str, team_id: str) -> None:
        self._team_id = team_id
        self._session = create_session(
            headers={
                "Authorization": api_key,
                "Content-Type": "application/json",
            }
        )

    def create_issue(self, request: CreateIssueRequest) -> Issue:
        """Create a new Linear issue in the configured team.

        Args:
            request: Title, description, priority, labels, and optional assignee.

        Returns:
            The newly created Issue populated from the API response.

        Raises:
            IssueTrackerError: If the GraphQL mutation fails.
        """
        priority_num = _PRIORITY_TO_LINEAR[request.priority]
        mutation = """
        mutation CreateIssue($input: IssueCreateInput!) {
          issueCreate(input: $input) {
            success
            issue {
              id title description priority url
              state { name type }
              assignee { name email }
              labels { nodes { name } }
            }
          }
        }
        """
        variables: dict[str, Any] = {
            "input": {
                "teamId": self._team_id,
                "title": request.title,
                "description": request.description,
                "priority": priority_num,
            }
        }
        if request.assignee:
            variables["input"]["assigneeId"] = request.assignee

        data = _gql(self._session, mutation, variables)
        result = data.get("issueCreate", {})
        if not result.get("success"):
            raise IssueTrackerError(f"Linear issueCreate returned success=false for title '{request.title}'")

        issue_raw: dict[str, Any] = result.get("issue") or {}
        logger.info("Created Linear issue %s: %s", issue_raw.get("id"), request.title)
        return _parse_issue(issue_raw)

    def get_issue(self, issue_id: str) -> Issue | None:
        """Fetch a single Linear issue by its UUID.

        Args:
            issue_id: The Linear issue UUID string.

        Returns:
            The Issue if found, or None if Linear returns a not-found error.

        Raises:
            IssueTrackerError: On connection failure or unexpected API errors.
        """
        query = """
        query GetIssue($id: String!) {
          issue(id: $id) {
            id title description priority url
            state { name type }
            assignee { name email }
            labels { nodes { name } }
          }
        }
        """
        try:
            data = _gql(self._session, query, {"id": issue_id})
        except IssueTrackerError as exc:
            # Linear returns a GraphQL error (not HTTP 404) for missing issues
            if "not found" in str(exc).lower() or "could not be found" in str(exc).lower():
                return None
            raise

        issue_raw = data.get("issue")
        if issue_raw is None:
            return None
        return _parse_issue(issue_raw)

    def list_issues(self, status: IssueStatus | None = None, limit: int = 50) -> list[Issue]:
        """List issues for the configured team.

        Args:
            status: Filter by normalized status. None returns all states.
            limit: Maximum number of issues (capped at 250 per Linear pagination).

        Returns:
            List of Issue objects.

        Raises:
            IssueTrackerError: On connection failure or API errors.
        """
        capped_limit = min(limit, 250)
        query = """
        query ListIssues($teamId: String!, $first: Int!) {
          team(id: $teamId) {
            issues(first: $first, orderBy: updatedAt) {
              nodes {
                id title description priority url
                state { name type }
                assignee { name email }
                labels { nodes { name } }
              }
            }
          }
        }
        """
        data = _gql(self._session, query, {"teamId": self._team_id, "first": capped_limit})
        team_data = data.get("team") or {}
        nodes: list[dict[str, Any]] = (team_data.get("issues") or {}).get("nodes", [])

        issues = [_parse_issue(node) for node in nodes]
        if status is not None:
            issues = [iss for iss in issues if iss.status == status]
        return issues[:limit]

    def update_status(self, issue_id: str, status: IssueStatus) -> bool:
        """Transition a Linear issue by querying for a matching state name.

        Linear requires a state ID rather than a state name. This method
        queries the team's workflow states to find a matching state, then
        applies it.

        Args:
            issue_id: Linear issue UUID.
            status: Target normalized status.

        Returns:
            True if updated, False if the issue was not found.

        Raises:
            IssueTrackerError: On connection failure or when no matching state exists.
        """
        # Map normalized status to Linear state type for the lookup
        target_type = {
            IssueStatus.OPEN: "unstarted",
            IssueStatus.IN_PROGRESS: "started",
            IssueStatus.RESOLVED: "completed",
            IssueStatus.CLOSED: "cancelled",
        }[status]

        states_query = """
        query GetStates($teamId: String!) {
          team(id: $teamId) {
            states { nodes { id name type } }
          }
        }
        """
        data = _gql(self._session, states_query, {"teamId": self._team_id})
        team_data = data.get("team") or {}
        state_nodes: list[dict[str, Any]] = (team_data.get("states") or {}).get("nodes", [])

        state_id = None
        for node in state_nodes:
            if node.get("type", "").lower() == target_type:
                state_id = node.get("id")
                break

        if not state_id:
            raise IssueTrackerError(f"No Linear workflow state of type '{target_type}' found in team {self._team_id}")

        mutation = """
        mutation UpdateIssue($id: String!, $stateId: String!) {
          issueUpdate(id: $id, input: { stateId: $stateId }) {
            success
          }
        }
        """
        try:
            result_data = _gql(self._session, mutation, {"id": issue_id, "stateId": state_id})
        except IssueTrackerError as exc:
            if "not found" in str(exc).lower():
                return False
            raise

        return bool((result_data.get("issueUpdate") or {}).get("success"))

    def add_comment(self, issue_id: str, comment: str) -> bool:
        """Add a comment to a Linear issue.

        Args:
            issue_id: Linear issue UUID.
            comment: Markdown-formatted comment body.

        Returns:
            True if created, False if the issue was not found.

        Raises:
            IssueTrackerError: On connection failure or unexpected API errors.
        """
        mutation = """
        mutation CreateComment($issueId: String!, $body: String!) {
          commentCreate(input: { issueId: $issueId, body: $body }) {
            success
          }
        }
        """
        try:
            data = _gql(self._session, mutation, {"issueId": issue_id, "body": comment})
        except IssueTrackerError as exc:
            if "not found" in str(exc).lower():
                return False
            raise

        return bool((data.get("commentCreate") or {}).get("success"))
