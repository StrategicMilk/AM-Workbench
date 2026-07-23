# Workbench Gateway Policy Reference

## Overview

The gateway policy is the fail-closed guardrail layer that evaluates task
submissions against configured policy rules before they enter execution. Tasks
that fail policy evaluation are rejected with a typed error.

## Policy Evaluation Model

- The gateway runs synchronously on the task-submission path before queue
  insertion.
- The result is `allowed` when the task may proceed or `rejected` when the
  caller should receive a typed rejection.
- Evaluation exceptions fail closed: the task is treated as `rejected`, never
  approved by default.

## Configurable Policy Rules

Policy rules can include model allowlists, token budget ceilings, and rate
limits. Use the [configuration key reference](config-keys.md) for the current
key names; do not assume a key exists until it is listed there.

## Reading Rejection Reasons

Rejection responses include a reason code and operator-facing explanation.
Operators can also inspect `outputs/logs/` and the Workbench UI for the
recorded rejection reason and correlated run or task identifier.

## Modifying Policy Rules

Policy changes require a server restart to take effect. Update only documented
keys from [configuration keys](config-keys.md), restart the server, and verify a
known-good request still passes while a known-bad request is rejected.

## Security Note

The gateway policy is fail-closed. An evaluation exception causes rejection,
not approval. Do not disable or bypass the gateway in production.
