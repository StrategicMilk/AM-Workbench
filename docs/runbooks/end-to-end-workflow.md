# End-to-End AM Workbench Workflow Runbook

This runbook demonstrates the "Run" golden path - one of four canonical user journeys (Run / Inspect / Experiment / Promote) - through AM Workbench's workbench layer,
covering plan generation, approval gating, agent execution, memory logging, and output assembly.

---

## Prerequisites

- AM Workbench installed: `pip install -e .`
- UnifiedMemoryStore initialized (default — no extra configuration required)
- Plan mode enabled (default — controlled by `PLAN_MODE_ENABLE`)
- Admin token configured (`VETINARI_ADMIN_TOKEN`)
- At least one inference backend reachable: llama-cpp GGUF files under `VETINARI_MODELS_DIR`, a hosted provider API key (Anthropic/OpenAI/Gemini/Cohere/HuggingFace/Replicate), or another backend declared in `config/backend_pins.yaml`

### Cloud Provider Credentials (Optional)

```bash
# .env file
OPENAI_API_KEY="sk-..."
ANTHROPIC_API_KEY="sk-ant-..."
GEMINI_API_KEY="AIza..."
COHERE_API_KEY="co_..."
VETINARI_MODELS_DIR="./models"
```

Or use the credential vault:

```python
from vetinari.credentials import get_credential_manager

cm = get_credential_manager()
cm.set_credential("openai", "sk-...", credential_type="bearer", note="OpenAI GPT-4")
```

### Starting the Server

AM Workbench's migrated API-domain surface runs on the native Rust kernel
server and defaults to port 5000:

```bash
python -m vetinari serve
# or
python -m vetinari start
```

The server listens on `http://localhost:5000` by default. Override with:

```bash
VETINARI_WEB_PORT=8080 python -m vetinari serve
```

Protected sibling surfaces such as approvals, skills, SSE, MCP, and training
may still use retained compatibility modules, but they are not the live host for
the migrated AM Workbench API domains.

```bash
cargo run -p amw-kernel --bin amw-kernel-server -- --host 127.0.0.1 --port 5000
```

---

## Core Workflow: Plan, Approve, Execute, Verify

All AM Workbench workflows follow this pattern:

```
User Input → Plan (Foreman) → Risk Assessment → Approval → Execution (Worker) → Quality Gate (Inspector) → Output Assembly
```

The three agents in the pipeline are:

- **Foreman** (`ForemanAgent`) — decomposes the goal into a task DAG, manages clarification and context
- **Worker** (`WorkerAgent`) — executes tasks across 24 modes in 4 groups (Research, Architecture, Build, Operations)
- **Inspector** (`InspectorAgent`) — independently verifies quality via code review, security audit, and test generation

---

### Step 1: Generate a Plan

```bash
curl -X POST http://localhost:5000/api/v1/plans \
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Python calculator module",
    "prompt": "Create a Python calculator module with add, sub, mul, div methods and pytest tests",
    "created_by": "user"
  }'
```

Response (HTTP 201):
```json
{
  "plan_id": "plan_abc123",
  "title": "Python calculator module",
  "status": "draft",
  "risk_score": 0.15,
  "subtasks": [...]
}
```

To generate a plan via the PlanModeEngine directly (e.g., in scripts):

```python
from vetinari.planning.plan_mode import PlanModeEngine
from vetinari.planning.plan_types import PlanGenerationRequest

engine = PlanModeEngine()
plan = engine.generate_plan(
    PlanGenerationRequest(
        goal="Create a Python calculator module with tests",
        domain_hint="coding",
    )
)
print(f"Plan ID: {plan.plan_id}, risk: {plan.risk_score:.2f}")
```

### Step 2: Check Approval Requirements

```bash
curl -X GET http://localhost:5000/api/v1/plans/plan_abc123 \
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN"
```

Risk scoring determines approval requirements:

- **LOW** (0.0–0.25): Auto-approved in dry-run mode
- **MEDIUM** (0.25–0.5): May require approval depending on `DRY_RUN_RISK_THRESHOLD`
- **HIGH** (0.5–0.75): Requires human approval
- **CRITICAL** (0.75–1.0): Requires explicit approval before any execution

Check pending approvals via the local approvals API:

```bash
curl -X GET http://localhost:5000/api/v1/approvals/pending \
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN"
```

### Step 3: Approve a Pending Action

```bash
curl -X POST http://localhost:5000/api/v1/approvals/ACTION_ID/approve \
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN"
```

To reject:

```bash
curl -X POST http://localhost:5000/api/v1/approvals/ACTION_ID/reject \
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason": "Scope exceeds approved budget"}'
```

### Step 4: Start Plan Execution

```bash
curl -X POST http://localhost:5000/api/v1/plans/plan_abc123/start \
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN"
```

Response:
```json
{
  "plan_id": "plan_abc123",
  "status": "running",
  "started_at": "2026-04-06T10:00:00+00:00"
}
```

### Approval JSON Schemas

**Plan action approval:**
```json
{
  "action_id": "string (required, from pending list)",
  "status": "approved | rejected (set by the endpoint)",
  "reason": "string (optional, for rejections)"
}
```

**Subtask-level approval (via decision API):**
```json
{
  "decision_id": "string (required)",
  "resolution": "approve | reject",
  "reason": "string (optional)"
}
```

---

## Workflow A: Full Pipeline via Python

Execute the complete pipeline — plan generation, approval, coding execution, and memory logging — in a single script.

```python
"""End-to-end example: PlanModeEngine → coding task execution → memory logging."""

from vetinari.structured_logging import configure_logging, CorrelationContext, get_logger
from vetinari.planning.plan_mode import PlanModeEngine
from vetinari.planning.plan_types import PlanApprovalRequest, PlanGenerationRequest
from vetinari.memory.unified import get_unified_memory_store
from vetinari.memory.interfaces import MemoryEntry
from vetinari.types import MemoryType

configure_logging()
logger = get_logger("example")


def main() -> None:
    engine = PlanModeEngine()
    memory = get_unified_memory_store()

    with CorrelationContext():
        # Foreman: decompose goal into a subtask DAG
        plan = engine.generate_plan(
            PlanGenerationRequest(
                goal="Add a health-check endpoint to the demo service",
                domain_hint="coding",
            )
        )
        logger.info("Generated plan %s (risk=%.2f)", plan.plan_id, plan.risk_score)

        # Low-risk plans can be approved programmatically
        if plan.risk_score < 0.25:
            plan = engine.approve_plan(
                PlanApprovalRequest(
                    plan_id=plan.plan_id,
                    approved=True,
                    approver="admin",
                )
            )

            # Worker: execute all coding subtasks in the plan
            results = engine.execute_multi_step_coding(plan, plan.subtasks)

            for result in results:
                if result.get("success"):
                    memory.remember(
                        MemoryEntry(
                            content=result.get("output", ""),
                            entry_type=MemoryType.TASK_RESULT,
                            agent="worker",
                            metadata={
                                "subtask_id": result.get("subtask_id"),
                                "plan_id": plan.plan_id,
                            },
                        )
                    )

        logger.info("Workflow complete — results stored in UnifiedMemoryStore")


if __name__ == "__main__":
    main()
```

### Querying Logged Results

```python
from vetinari.memory.unified import get_unified_memory_store

memory = get_unified_memory_store()

# Full-text search
results = memory.search("health-check endpoint", limit=10)
for entry in results:
    print(f"[{entry.entry_type.value}] agent={entry.agent}  id={entry.id}")

# Semantic search (requires embedding endpoint)
results = memory.ask("What did the worker produce for the health check task?")
```

---

## Workflow B: Worker Modes

The Worker supports 24 modes grouped into 4 categories. Specify the mode when routing a task:

| Group | Modes |
|-------|-------|
| **Research** (read-only + web) | `code_discovery`, `domain_research`, `api_lookup`, `lateral_thinking`, `ui_design`, `database`, `devops`, `git_workflow` |
| **Architecture** (read-only + ADR production) | `architecture`, `risk_assessment`, `ontological_analysis`, `contrarian_review`, `suggest` |
| **Build** (sole file writer) | `build`, `image_generation` |
| **Operations** (post-execution synthesis) | `documentation`, `creative_writing`, `cost_analysis`, `experiment`, `error_recovery`, `synthesis`, `improvement`, `monitor`, `devops_ops` |

```python
from vetinari.agents.consolidated.worker_agent import WorkerAgent
from vetinari.agents.contracts import AgentTask
from vetinari.types import AgentType

worker = WorkerAgent()

# Research mode — read-only, no file writes
# Mode is passed via context["mode"] so the MultiModeAgent router picks it up
task = AgentTask(
    task_id="research_001",
    agent_type=AgentType.WORKER,
    description="Research best practices for Python health-check endpoints",
    prompt="Research best practices for Python health-check endpoints",
    context={"mode": "domain_research"},
)
result = worker.execute(task)
print(result.output)

# Build mode — writes production files
build_task = AgentTask(
    task_id="build_001",
    agent_type=AgentType.WORKER,
    description="Implement the /health endpoint in app.py",
    prompt="Implement the /health endpoint in app.py",
    context={"mode": "build", "target_file": "app.py"},
)
build_result = worker.execute(build_task)
```

---

## Workflow C: Inspector Quality Gate

The Inspector runs after Worker execution to verify correctness, security, and test coverage.

```python
from vetinari.agents.consolidated.quality_agent import InspectorAgent
from vetinari.agents.contracts import AgentTask
from vetinari.types import AgentType

inspector = InspectorAgent()

# Code review
review_task = AgentTask(
    task_id="review_001",
    agent_type=AgentType.INSPECTOR,
    description="Review app.py for correctness and maintainability",
    prompt="Review app.py for correctness and maintainability",
    context={"mode": "code_review", "file_path": "app.py"},
)
review = inspector.execute(review_task)
print(f"Quality gate passed: {review.success}")
print(review.output)

# Security audit
security_task = AgentTask(
    task_id="security_001",
    agent_type=AgentType.INSPECTOR,
    description="Audit app.py for vulnerabilities",
    prompt="Audit app.py for vulnerabilities",
    context={"mode": "security_audit", "file_path": "app.py"},
)
audit = inspector.execute(security_task)
```

Inspector modes:

| Mode | Purpose |
|------|---------|
| `code_review` | General quality, design patterns, maintainability |
| `security_audit` | Vulnerability detection — 45+ heuristic patterns + LLM |
| `test_generation` | pytest-aware test generation with coverage analysis |

---

## Secret Filtering

UnifiedMemoryStore automatically scans and redacts secrets before persisting entries.

```python
from vetinari.memory.unified import get_unified_memory_store
from vetinari.memory.interfaces import MemoryEntry
from vetinari.types import MemoryType

memory = get_unified_memory_store()

entry = MemoryEntry(
    content="api_key: sk-proj-1234567890abcdefghijk",
    entry_type=MemoryType.CONFIG,
    agent="example",
    metadata={"type": "api_config"},
)
entry_id = memory.remember(entry)

# Verify the secret was redacted before storage
retrieved = memory.get_entry(entry_id)
assert "[REDACTED]" in retrieved.content
```

---

## Distributed Tracing

Wrap any workflow in `CorrelationContext` to propagate a trace ID through all log lines:

```python
from vetinari.structured_logging import CorrelationContext

with CorrelationContext() as ctx:
    logger.info("Starting workflow — trace_id=%s", ctx.trace_id)
    # ... all log statements inside this block include the trace_id
```

Viewing traces in JSON log output:

```bash
# View all trace IDs
cat logs/vetinari.log | jq '.trace_id'

# Filter to a single trace
cat logs/vetinari.log | jq 'select(.trace_id == "YOUR_TRACE_ID")'
```

---

## Lifecycle Controls

| Action | Endpoint |
|--------|---------|
| List all plans | `GET /api/v1/plans` |
| Get plan details | `GET /api/v1/plans/{plan_id}` |
| Start plan | `POST /api/v1/plans/{plan_id}/start` |
| Pause running plan | `POST /api/v1/plans/{plan_id}/pause` |
| Resume paused plan | `POST /api/v1/plans/{plan_id}/resume` |
| Cancel plan | `POST /api/v1/plans/{plan_id}/cancel` |
| Plan status | `GET /api/v1/plans/{plan_id}/status` |
| List pending approvals | `GET /api/v1/approvals/pending` |
| Approve action | `POST /api/v1/approvals/{action_id}/approve` |
| Reject action | `POST /api/v1/approvals/{action_id}/reject` |
| Agent status | `GET /api/v1/agents/status` |
| Memory entries | `GET /api/v1/agents/memory` |
| Pending decisions | `GET /api/v1/decisions/pending` |
| Submit decision | `POST /api/v1/decisions` |

All `POST` routes that modify state require an admin token:

```bash
-H "Authorization: Bearer $VETINARI_ADMIN_TOKEN"
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Plan mode disabled | Set `PLAN_MODE_ENABLE=true` and `PLAN_MODE_DEFAULT=true` |
| No models found | If using llama-cpp: set `VETINARI_MODELS_DIR` and verify `.gguf` files are present. For hosted providers: confirm the API key is set. For other backends: confirm the backend is declared in `config/backend_pins.yaml` and its configured endpoint/model resolves. |
| Memory store not initializing | Check write permissions on `vetinari_memory.db`; inspect logs for `StorageError` |
| Approval not persisting | Verify `VETINARI_ADMIN_TOKEN` is set and matching |
| No trace IDs in logs | Wrap execution in `CorrelationContext()` |
| Secret filtering not working | Check `vetinari/security/__init__.py` for `SecretScanner` patterns |
| API routes unavailable | Reinstall from project metadata with `python -m pip install -e ".[dev,local,ml,search,notifications]"`, rebuild the Rust kernel with `cargo build -p amw-kernel`, and restart `vetinari serve` |
| Worker mode not recognized | Check mode name against the 24-mode table above |
| Inspector security audit slow | 45+ heuristic patterns run synchronously — expected on large files |

---

## Related Documents

- [Memory System Reference](../reference/memory.md) — UnifiedMemoryStore architecture and API
- [Approval Workflow Reference](../reference/approval-workflow.md) — Approval states and API
- [Configuration Reference](../reference/config.md) — Environment variables
- [Dashboard Guide](dashboard-guide.md) — Monitoring dashboard operations
- [Ponder Runbook](ponder.md) — Model selection operations
