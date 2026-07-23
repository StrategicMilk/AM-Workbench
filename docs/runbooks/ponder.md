# Ponder Orchestration Runbook

Ponder is currently an in-process model-selection helper for AM Workbench, not a
mounted HTTP API in this checkout. The live contract is documented in
`docs/api/ponder.md`; the implementation lives in `vetinari/models/ponder.py`
and `vetinari/models/ponder_scoring.py`.

## Current Operations

- Use `ponder_project_for_plan(plan_id)` for a plan-wide model-selection pass.
- Use `get_ponder_results_for_plan(plan_id)` to inspect stored per-subtask
  rankings and scores.
- Use `get_ponder_health()` to inspect model-discovery enablement, cloud weight,
  and provider health.
- Use `PonderEngine.get_template_prompts()` for the current in-process prompt
  list.

The removed `POST /api/ponder/plan/<plan_id>`, `GET /api/ponder/plan/<plan_id>`,
`GET /api/ponder/health`, `POST /api/ponder/choose-model`, and
`GET /api/ponder/templates` endpoints must not be treated as live until a route
module is restored, authenticated, tested, and added to the route matrix.

## Configuration

Cloud-provider signals are optional. The system degrades to local-only scoring
when provider tokens are absent.

```bash
export HF_HUB_TOKEN=your_huggingface_token
export REPLICATE_API_TOKEN=your_replicate_token
export CLAUDE_API_KEY=your_anthropic_key
export GEMINI_API_KEY=your_google_key
export ENABLE_PONDER_MODEL_DISCOVERY=true
export PONDER_CLOUD_WEIGHT=0.20
```

## Troubleshooting

- Empty Ponder results: inspect `get_ponder_health()` and verify subtasks have
  `ponder_ranking` or `ponder_scores` fields.
- Stale cloud signals: rerun `get_ponder_health()` and confirm whether model
  discovery is enabled. The current mounted API does not expose a cache-clear
  endpoint.
- Legacy subtask records: run the migration module with Python module execution
  if you need to upgrade serialized legacy payloads:

```bash
python -m vetinari.migrations.upgrade_subtask_schema_v1_to_v2 path/to/legacy-subtasks.json
```

## Re-Exposing HTTP

If Ponder is mounted again as HTTP, the implementation must add a live route file
under `vetinari/web/`, wire authentication and request validation, add route
tests, and update `docs/security/route-auth-matrix.md` before this runbook may
describe endpoints as live.

## RCG-0065-P02 Security Doc Evidence

- Source rows: FSA-9668.
- Validation command: `.venv312/Scripts/python.exe -m pytest tests/test_rcg_0065_p02.py`.
- Evidence contract: ponder runbook security documentation evidence remains
  closed only while the P02 closure test verifies the row, this marker, and the
  validation command. Unknown, missing, stale, or unreadable evidence remains
  open in the closure artifacts.
