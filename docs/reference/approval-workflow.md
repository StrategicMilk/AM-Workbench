# Plan Approval Workflow

This document describes the approval workflow for coding tasks in AM Workbench's plan mode.

## Overview

AM Workbench implements a governance model where:
- **Plan mode**: Coding tasks require human approval before execution
- **Build mode**: Execution proceeds without blocking (governance checks still apply)

## Approval States

A task can be in one of the following approval states:

| State | Description | Can Proceed to Execution |
|-------|-------------|-------------------------|
| `pending` | Awaiting approval | No |
| `approved` | Approved by human or auto-approved | Yes |
| `rejected` | Rejected by human | No |
| `auto_approved` | System auto-approved (low risk) | Yes |

## Approval Entry Schema

When an approval decision is made, a `MemoryEntry` is created:

```python
@dataclass
class ApprovalEntry:
    agent: str = "plan-approval"
    entry_type: MemoryType = MemoryType.APPROVAL
    content: str  # JSON of ApprovalDetails
    summary: str  # Human-readable summary


@dataclass
class ApprovalDetails:
    task_id: str
    task_type: str  # "coding", "docs", "data_processing", etc.
    plan_id: str
    approval_status: str  # "approved", "rejected", "auto_approved"
    approver: str  # "system" or username
    reason: str  # Optional rejection reason
    risk_score: float
    timestamp: str  # ISO timestamp
```

## Workflow: Plan Mode

```
User Input → PlanAgent → Plan Generated → Risk Assessment
                                           ↓
                              Is coding task? ──No──→ Execute (no approval needed)
                              │
                              ↓Yes
                         Risk Score ≤ Threshold?
                              ↓                ↓
                         Yes (auto-approve)  No
                              ↓                ↓
                      Mark approved      Queue for human review
                              ↓                ↓
                      Log approval      Notify approver
                              ↓                ↓
                      Execute           Wait for decision
                              ↓                ↓
                         Complete         Approved? → Execute
                                              ↓
                                           Rejected → Stop
```

## Workflow: Build Mode

In Build mode, the governance model is relaxed:
- Coding tasks do NOT require human approval
- However, security and policy checks still apply
- Audit entries are still created for traceability

```
User Input → PlanAgent → Plan Generated → Risk Assessment
                                           ↓
                              Is coding task? ──No──→ Execute
                              │
                              ↓Yes
                         Policy checks (sandbox, secrets)
                              ↓
                         Pass? ──No──→ Log warning, block
                              ↓Yes
                         Log audit entry → Execute
```

## Approval API

### Request Approval

```http
POST /api/plan/{plan_id}/subtasks/{subtask_id}/approve
Authorization: Bearer {VETINARI_ADMIN_TOKEN}

{
    "approved": true,
    "approver": "username",
    "reason": "Looks good, proceed",
    "audit_id": "audit-123",
    "risk_score": 0.15
}
```

`approved` must be a JSON boolean and `approver` must be a string. Optional
audit fields such as `reason`, `audit_id`, `risk_score`, `timestamp`, and
`approval_schema_version` are request-contract fields, not presence-only flags;
do not send string values such as `"false"` for rejection.

### Plan-Level Approval

```http
POST /api/plan/{plan_id}/approve
Authorization: Bearer {VETINARI_ADMIN_TOKEN}

{
    "approved": false,
    "approver": "username",
    "reason": "Risk is too high"
}
```

### Pending Approval Queue

The mounted approval queue endpoints live under `/api/v1/approvals` and are
guarded by `admin_guard`:

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/approvals/pending` | List pending autonomy actions |
| `POST /api/v1/approvals/{action_id}/approve` | Approve a pending action |
| `POST /api/v1/approvals/{action_id}/reject` | Reject a pending action, optionally with `reason` |

Older `GET /api/plan/{plan_id}/subtasks/{subtask_id}/approval` and
`GET /api/plan/approvals/pending` routes are not mounted by
`litestar_plan_api.py` / `approvals_api.py`.

## Auto-Approval Rules

Tasks are auto-approved when:
1. `dry_run=true` AND `risk_score ≤ DRY_RUN_RISK_THRESHOLD` (default: 0.25)
2. Task type is NOT a coding task
3. Task is in Build mode (not Plan mode)

Environment variables:
- `DRY_RUN_RISK_THRESHOLD`: Default 0.25
- `PLAN_MODE_ENABLE`: Default true
- `PLAN_MODE_DEFAULT`: Default true (all tasks require plan-first)

## Approval Audit Trail

All approval decisions are logged to memory:

```python
# Example approval memory entry
{
    "agent": "plan-approval",
    "entry_type": "approval",
    "content": {
        "task_id": "subtask_001",
        "task_type": "coding",
        "plan_id": "plan_abc123",
        "approval_status": "approved",
        "approver": "john_doe",
        "reason": "Code review passed",
        "risk_score": 0.15,
        "timestamp": "2026-03-03T10:30:00Z",
    },
    "summary": "Approved coding task subtask_001 by john_doe",
    "timestamp": 1709478600000,
    "provenance": "plan_approval_api",
    "source_backends": ["oc", "mnemosyne"],
}
```

## Security Considerations

1. **Secrets Handling**: Approval entries should NEVER contain secrets
2. **Audit Retention**: Approval entries are retained per `PLAN_RETENTION_DAYS`
3. **Access Control**: Approval endpoints require `VETINARI_ADMIN_TOKEN` through `admin_guard`
4. **Rate Limiting**: Do not claim approval-spam protection until the route-level limiter proof exists
