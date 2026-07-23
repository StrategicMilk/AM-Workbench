# AM Workbench Ponder Contracts

Ponder is currently an in-process model-selection helper, not a mounted HTTP API in this checkout. The live source is `vetinari/models/ponder.py` and `vetinari/models/ponder_scoring.py`.

Do not treat the former Ponder HTTP paths as live endpoints until a current
route module is restored, authentication and request validation are wired, route
tests exist, and the route-to-test matrix is regenerated.

## Live Functions

- `ponder_project_for_plan(plan_id)` runs a plan-wide scoring pass for subtasks known to the planning subsystem.
- `get_ponder_results_for_plan(plan_id)` returns stored per-subtask Ponder rankings and scores.
- `get_ponder_health()` returns model-discovery enablement, cloud-weight configuration, and cloud-provider health.
- `rank_models(task_description, top_n)` scores local models for a task description.
- `PonderEngine.get_template_prompts()` returns the current in-process prompt list. The removed template-file catalog is not a route contract.

## Health Payload

`get_ponder_health()` returns this shape:

```json
{
  "enable_model_discovery": true,
  "cloud_weight": 0.2,
  "providers": {
    "provider_id": {
      "available": true,
      "name": "Provider Name",
      "has_token": true
    }
  }
}
```

## Ranking Payload

`rank_models(task_description, top_n)` returns this shape:

```json
{
  "task_id": "ponder_20240101_120000",
  "task_description": "Write Python code for data processing",
  "rankings": [
    {
      "rank": 1,
      "model_id": "qwen2.5-coder-14b-instruct",
      "model_name": "Qwen2.5 Coder 14B",
      "total_score": 0.95,
      "capability_score": 0.9,
      "context_score": 1.0,
      "memory_score": 1.0,
      "heuristic_score": 0.8,
      "policy_penalty": 0,
      "reasoning": "capability match: 0.90, context fit: 1.00"
    }
  ],
  "timestamp": "2024-01-01T12:00:00",
  "phase": "result"
}
```

## Route Restoration Requirement

If Ponder is mounted again as HTTP, the implementation must add a live route
file, wire authentication and request validation, add route tests, and update
`docs/security/route-auth-matrix.md` before this document may describe
endpoints as live.

## Security Notes

- Tokens must never be returned in API responses.
- Tokens are sourced from environment variables or configured provider credentials.
- Plan-level Ponder operations must be restricted to authorized operators if re-exposed over HTTP.
