"""Vetinari external integrations — issue trackers and third-party services.

Public surface for the integrations package. Import the abstract interface
and factory function from here rather than from the individual adapter modules.

Example::

    import yaml
    from pathlib import Path
    from vetinari.integrations import (
        CreateIssueRequest,
        Issue,
        IssuePriority,
        IssueStatus,
        IssueTracker,
        IssueTrackerError,
        create_issue_tracker,
    )

    cfg = yaml.safe_load(
        Path("config/integrations.yaml").read_text(encoding="utf-8")
    )
    tracker = create_issue_tracker(cfg)
    if tracker:
        issue = tracker.create_issue(
            CreateIssueRequest(title="Bug: agent failed", priority=IssuePriority.HIGH)
        )
"""

from __future__ import annotations

from vetinari.integrations.issue_tracker import (
    CreateIssueRequest,
    Issue,
    IssuePriority,
    IssueStatus,
    IssueTracker,
    IssueTrackerError,
    create_issue_tracker,
)

__all__ = [
    "CreateIssueRequest",
    "Issue",
    "IssuePriority",
    "IssueStatus",
    "IssueTracker",
    "IssueTrackerError",
    "create_issue_tracker",
]
