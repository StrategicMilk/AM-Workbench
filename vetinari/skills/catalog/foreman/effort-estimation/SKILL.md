---
name: Effort Estimation
description: Estimate token cost, wall-clock time, and complexity using caller-provided history, LLM-driven episode-memory notes, and quality scoring
mode: plan
agent: foreman
version: "1.0.0"
capabilities:
  - task_scheduling
  - specification
tags:
  - planning
  - estimation
  - cost
  - scheduling
---

# Effort Estimation

## Purpose

Effort Estimation produces confidence-rated predictions for token cost, wall-clock duration, and complexity tier for each task in a plan. It draws on caller-provided historical execution data, LLM-driven episode_memory notes when available, quality_scorer baselines, and structural analysis of the target codebase to calibrate estimates. The episode_memory step is a prose integration point, not a guaranteed Python API call; if no history is supplied or found, the estimate must say it is uncalibrated. Accurate estimation enables the Foreman to set realistic budgets, prioritize work within constraints, and alert users when a request exceeds available resources before committing any execution tokens.

## When to Use

- After task decomposition, to assign effort labels (XS/S/M/L/XL) to each task
- Before committing to a plan, to validate it fits within token and cost budgets
- When the user asks "how long will this take?" or "how much will this cost?"
- During replan-on-failure, to recalculate remaining budget after plan changes
- When comparing multiple decomposition strategies to pick the most efficient one
- For capacity planning across multiple concurrent plans

## Inputs

| Parameter        | Type            | Required | Description                                                         |
|------------------|-----------------|----------|---------------------------------------------------------------------|
| tasks            | list[dict]      | Yes      | Tasks to estimate (from task decomposition output)                  |
| context          | dict            | No       | Codebase context (size, complexity, language distribution)           |
| history          | list[dict]      | Yes      | Historical execution records supplied by the caller or LLM-driven episode_memory notes |
| model_config     | dict            | No       | Model tier configuration with per-token costs                       |
| budget           | dict            | No       | Available budget constraints (max_tokens, max_cost_usd, max_time)   |
| confidence_level | string          | No       | Desired confidence: "low" (50%), "medium" (75%), "high" (90%)       |

## Process Steps

1. **Task classification** -- Classify each task by archetype: research (read-heavy, low output), architecture (analysis, medium output), build (write-heavy, high output), review (read-heavy, structured output), documentation (write-heavy, narrative output). Each archetype has different token profiles.

2. **Historical lookup** -- Require caller-provided history or a successful LLM-driven episode_memory lookup for similar completed tasks. Match on: mode, file count, complexity tier, and description similarity. Extract actual token usage, wall-clock time, and pass/fail rates from historical records. If no history is available, return an uncalibrated estimate status instead of claiming memory-calibrated confidence.

3. **Structural analysis** -- For build tasks, analyze the target files: lines of code, number of functions, cyclomatic complexity, import depth. Larger and more complex files require more tokens for context and more tokens for output.

4. **Base estimate calculation** -- For each task, compute a base estimate using the archetype profile adjusted by structural analysis. If historical data exists, weight the historical average at 70% and the structural estimate at 30%.

5. **Thinking tier assignment** -- Based on task complexity, assign a thinking mode (low/medium/high/xhigh). Simple lookups get "low", standard implementations get "medium", complex architectural decisions get "high", novel problem-solving gets "xhigh". Each tier has a token multiplier.

6. **Model tier selection** -- Map each task to the appropriate model tier. Research and simple builds use efficient models, architecture and complex builds use capable models, security audits use the most capable model. Each model has different per-token costs.

7. **Confidence interval computation** -- Calculate confidence intervals based on historical variance. If no historical data exists, use wider intervals. Express as: P50 (expected), P75 (likely), P90 (worst case). The `confidence_level` parameter selects which percentile to report as the primary estimate.

8. **Critical path duration** -- Sum the estimates along the critical path to get the minimum wall-clock time. Add parallelism discount for tasks that can execute concurrently. This gives the expected total duration.

9. **Budget feasibility check** -- Compare total estimated cost against the available budget. If the estimate exceeds the budget, flag which tasks could be deferred, simplified, or pruned to fit within constraints.

10. **Estimate assembly** -- Compile per-task estimates with effort labels (XS/S/M/L/XL), token counts, cost projections, and duration predictions. Include aggregate plan totals and budget feasibility assessment.

## Output Format

The skill produces effort estimates for each task and aggregate plan totals:

```json
{
  "plan_id": "plan-abc123",
  "estimates": [
    {
      "task_id": "T1",
      "effort": "S",
      "tokens": {"input": 2000, "output": 500, "total": 2500},
      "cost_usd": {"p50": 0.03, "p75": 0.05, "p90": 0.08},
      "duration_seconds": {"p50": 15, "p75": 25, "p90": 40},
      "thinking_mode": "low",
      "model_tier": "efficient",
      "confidence": 0.85,
      "basis": "historical (12 similar tasks)"
    },
    {
      "task_id": "T3",
      "effort": "L",
      "tokens": {"input": 15000, "output": 3000, "total": 18000},
      "cost_usd": {"p50": 0.45, "p75": 0.65, "p90": 0.90},
      "duration_seconds": {"p50": 120, "p75": 180, "p90": 300},
      "thinking_mode": "high",
      "model_tier": "capable",
      "confidence": 0.60,
      "basis": "structural analysis (no historical match)"
    }
  ],
  "aggregate": {
    "total_tokens": {"p50": 45000, "p75": 62000, "p90": 85000},
    "total_cost_usd": {"p50": 1.20, "p75": 1.80, "p90": 2.50},
    "wall_clock_seconds": {"p50": 300, "p75": 450, "p90": 720},
    "critical_path_tasks": ["T1", "T3", "T5", "T7"],
    "parallelism_factor": 1.8
  },
  "budget_feasibility": {
    "within_budget": true,
    "budget_remaining_usd": 2.50,
    "headroom_percent": 52
  }
}
```

## Quality Standards

This skill is governed by the following standards from the skill registry:

- **STD-FMN-001**: Every generated task MUST have a unique ID and an assigned_agent
- **STD-FMN-003**: Plans MUST include at least one verification task per implementation task (affects total cost)
- **STD-UNI-001**: All skill outputs MUST conform to the declared output_schema
- **STD-UNI-004**: Skill executions SHOULD be idempotent -- running twice with same input produces same output
- **CON-UNI-002**: All skills must respect their max_tokens and timeout_seconds limits
- **CON-UNI-003**: All skills must respect their max_cost_usd budget per invocation
- **GDL-FMN-003**: Include effort estimates (XS/S/M/L/XL) on each task for capacity planning

## Examples

### Example: Estimating a medium-complexity feature

**Input:**
```
tasks:
  - {id: "T1", description: "Research existing auth middleware", assigned_agent: "WORKER", mode: "code_discovery"}
  - {id: "T2", description: "Design JWT token validation flow", assigned_agent: "WORKER", mode: "architecture"}
  - {id: "T3", description: "Implement JWT middleware", assigned_agent: "WORKER", mode: "build"}
  - {id: "T4", description: "Write tests for JWT middleware", assigned_agent: "WORKER", mode: "build"}
  - {id: "T5", description: "Security audit of JWT implementation", assigned_agent: "INSPECTOR", mode: "security_audit"}
history: [12 prior auth-related tasks supplied by caller or LLM-driven episode_memory notes]
budget: {max_cost_usd: 5.00, max_time_seconds: 3600}
```

**Output (abbreviated):**
```
estimates:
  T1: effort=XS, cost=$0.03, duration=15s, confidence=0.90 (strong historical match)
  T2: effort=M, cost=$0.35, duration=90s, confidence=0.70 (moderate match)
  T3: effort=L, cost=$0.65, duration=180s, confidence=0.65 (structural estimate)
  T4: effort=M, cost=$0.40, duration=120s, confidence=0.75 (historical match)
  T5: effort=M, cost=$0.50, duration=90s, confidence=0.80 (standard audit)

aggregate:
  total_cost: $1.93 (p50), $2.80 (p75), $3.90 (p90)
  wall_clock: 8min (p50), 12min (p75), 18min (p90)
  critical_path: T1 -> T2 -> T3 -> T5

budget_feasibility:
  within_budget: true
  headroom: 61% cost, 83% time
```
