# Inspecting Run Results

## Overview

Workbench run results are recorded through the metadata spine, including JSONL
records under `outputs/workbench/spine/` and the local SQLite index used by the
Workbench read surfaces. These records connect task execution, status changes,
evidence assets, and operator-visible run summaries.

## Accessing Results Via The Web UI

Navigate to `http://localhost:8000/workbench`. The run list should show the
run ID, status, start and end time, and task count. Open a run detail view to
inspect step outcomes, evidence links, guardrail blocks, and recovery actions.

## Accessing Results Via The CLI

Use the Workbench CLI surface when it is available:

```powershell
python -m vetinari workbench list
python -m vetinari workbench inspect <run_id>
```

If the command is unavailable in the current build, use the web UI and spine
records instead of assuming the run succeeded.

## Reading The JSONL Spine Directly

The spine records live under `outputs/workbench/spine/`. Each line is a JSON
object with fields such as `state_after`, `payload`, and `timestamp`. For live
monitoring, tail the newest JSONL file and preserve the exact line when
escalating an incomplete run.

## Understanding Result States

| State | Operator meaning |
|---|---|
| `pending` | Accepted but not yet executing. |
| `running` | Work is in progress. |
| `completed` | The run reached a success terminal state. |
| `failed` | Execution ended with a concrete error. |
| `partial` | Some outputs are incomplete or missing; inspect evidence before reuse. |

## Troubleshooting Incomplete Results

Use [Troubleshooting](../troubleshooting.md) and preserve the run ID, spine
record, logs, and any Workbench-visible recovery action before retrying.
