# AM Workbench Pipeline

This page is the current quick-start reference for the runtime pipeline.

## Runtime Shape

AM Workbench uses a 3-agent factory pipeline:

```text
Foreman -> Worker -> Inspector
```

The core pipeline enum is `vetinari.types.AgentType`: `FOREMAN`, `WORKER`, and `INSPECTOR`. Three auxiliary types extend this for non-pipeline roles: `TRAINING` (fine-tuning/DPO/SimPO runners), `RELEASE` (release-certifier/publication boundary runners), and `WORKBENCH` (Workbench subsystem-scoped agent interactions). Auxiliary types do not participate in the Foreman→Worker→Inspector execution cycle.

## Agents And Modes

| Agent | Responsibility | Modes |
|---|---|---|
| Foreman | Plans, clarifies, consolidates context, owns the task graph | 6 |
| Worker | Executes research, architecture, build, operations, and recovery work | 24 |
| Inspector | Reviews quality, security, tests, and simplification | 4 |

See `vetinari/agents/` for the full mode catalog.

## Common Paths

```text
Express:
  Worker(task-specific mode) -> Inspector(review mode)

Standard:
  Foreman(plan) -> Worker(research/build as needed) -> Inspector -> Worker(documentation/synthesis as needed)

Custom:
  Foreman(clarify -> plan) -> Worker(research -> architecture -> build as needed) -> Inspector -> Worker(documentation/synthesis/improvement as needed)
```

Legacy names such as `Researcher`, `Oracle`, `Builder`, `Quality`, and `Operations` may appear in historical plans or as internal implementation class names. They are not current public runtime agent identities.

## Prompt And Rules Note

Runtime prompts can come from more than one path: agent markdown, prompt evolution, `PromptAssembler`, rules, learned failure patterns, and recalled examples. Frontmatter fields in `vetinari/config/agents/*.md` are metadata, not runtime model or tool enforcement.
