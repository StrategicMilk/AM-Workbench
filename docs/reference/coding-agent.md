# AM Workbench Coding Agent

The AM Workbench Coding Agent is an in-process coding agent that can generate code scaffolds, implementations, tests, and reviews. It integrates with AM Workbench's plan mode and memory system.

## Architecture

### Components

1. **CodeAgentEngine** (`vetinari/coding_agent/engine.py`)
   - In-process coding agent using internal LM
   - Generates scaffolds, implementations, tests, and reviews
   - Multi-step task support

2. **Execution helpers** (`vetinari/coding_agent/engine_execution.py`)
   - Validate generated artifacts before they are returned
   - Keep generated file writes scoped to the requested target files

3. **Generation helpers** (`vetinari/coding_agent/engine_generation.py`)
   - Produce scaffold, implementation, test, review, refactor, and fix artifacts

4. **Data Models** (`vetinari/coding_agent/engine_models.py`)
   - `make_code_agent_task`: public factory for coding `AgentTask` objects
   - `CodeArtifact`: Generated code artifact

## Usage

### Basic Usage

```python
from vetinari.coding_agent import CodeAgentEngine, CodingTaskType, make_code_agent_task

agent = CodeAgentEngine()

# Create a scaffold task
task = make_code_agent_task(
    "scaffold my_module",
    task_type=CodingTaskType.SCAFFOLD,
    language="python",
    target_files=["my_module"],
)

# Execute
artifact = agent.run_task(task)
print(artifact.path)  # Generated file path
print(artifact.content)  # Generated code
```

### Multi-Step Tasks

```python
tasks = [
    make_code_agent_task("scaffold demo", task_type=CodingTaskType.SCAFFOLD, target_files=["demo"]),
    make_code_agent_task("implement demo", task_type=CodingTaskType.IMPLEMENT, target_files=["demo"]),
    make_code_agent_task("test demo", task_type=CodingTaskType.TEST, target_files=["demo"]),
]

artifacts = agent.run_multi_step_task(tasks)
```

### Runtime Entry Points

The live coding-agent surface is the in-process Python engine and the plan
executor integration. This checkout does not expose HTTP coding-agent routes;
callers must use `CodeAgentEngine` directly or route approved plan-mode
subtasks through `PlanExecutor`.

## Configuration

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `CODING_AGENT_ENABLED` | `true` | Enable the coding agent |

## Task Types

- **SCAFFOLD**: Generate project skeleton
- **IMPLEMENT**: Generate implementation code
- **TEST**: Generate unit tests
- **REVIEW**: Generate code review
- **REFACTOR**: Generate refactored code
- **FIX**: Generate bug fix

## Integration with Plan Mode

The coding agent integrates with AM Workbench's plan mode:

1. Plan includes coding subtasks
2. Approval required for coding tasks (in Plan mode)
3. After approval, coding agent executes tasks
4. Artifacts logged to UnifiedMemoryStore

```python
from vetinari.planning.plan_executor import PlanExecutor

executor = PlanExecutor()

# Execute coding task as part of plan
result = executor.execute_coding_task(plan, subtask)
print(result["artifact"])
```

## Memory Integration

Coding artifacts are logged to UnifiedMemoryStore (SQLite + FTS5):

- Entry type: `FEATURE`
- Agent: `coding_agent`
- Provenance: `plan:{plan_id},task:{task_id}`

## Security

- Plan gating applies to all coding tasks
- Approvals logged with audit trail
- Generated artifacts are validated and scoped to requested target files before
  return; sandbox execution must not be claimed unless it is wired through the
  current runtime path and covered by tests.

## Runtime Ownership Notes

The coding-agent runtime contract is owned by the coding-agent implementation
and test surface: `vetinari/coding_agent/engine.py`,
`vetinari/adapter_manager.py`, and `tests/test_coding_agent.py`. This reference
page can describe the public shape, but stale behavior in those runtime/test
anchors must be fixed in a coding-agent runtime pack rather than treated as a
documentation-only closure.
