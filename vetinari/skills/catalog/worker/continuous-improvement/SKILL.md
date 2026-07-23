---
name: Continuous Improvement
description: PDCA cycles via the kaizen system to identify improvement opportunities, propose changes, and measure impact
mode: improvement
agent: worker
version: "1.0.0"
capabilities:
  - continuous_improvement
tags:
  - operations
  - kaizen
  - pdca
  - improvement
---

# Continuous Improvement

## Purpose

Continuous Improvement implements the Plan-Do-Check-Act (PDCA) cycle for the Vetinari system itself. It identifies improvement opportunities from execution data (failure patterns, cost trends, quality scores), proposes concrete changes, measures their impact after implementation, and feeds results back into the next improvement cycle. This skill embodies the kaizen philosophy -- continuous, incremental improvement as a first-class system capability, not an afterthought. It ensures Vetinari gets better at its job over time by learning from its own performance data.

## When to Use

- After completing a major plan, to identify what worked and what did not
- When quality scores show a declining trend
- When cost or duration metrics exceed targets
- At regular intervals (e.g., weekly) as a scheduled improvement review
- When the same type of failure occurs repeatedly
- When new patterns or tools become available that could improve existing processes
- After the system has accumulated enough episode_memory data for meaningful analysis

## Inputs

| Parameter       | Type            | Required | Description                                                        |
|-----------------|-----------------|----------|--------------------------------------------------------------------|
| task            | string          | Yes      | What area to improve and the improvement objective                 |
| metrics         | dict            | Yes      | Current performance metrics (quality scores, costs, durations)     |
| history         | list[dict]      | Yes      | Historical execution data from episode_memory                      |
| focus_area      | string          | No       | Specific area: "quality", "cost", "speed", "reliability"           |
| prior_cycles    | list[dict]      | No       | Results from previous improvement cycles                           |
| context         | dict            | No       | System configuration, constraints, goals                           |

## Process Steps

1. **PLAN: Data collection** -- Require explicit current metrics and historical execution records. If either input is missing or too sparse for comparison, return `success=false` with the missing input list instead of proposing improvements from anecdotes:
   - Episode memory: task completion rates, durations, retry counts
   - Quality scores: Inspector pass rates, review scores, defect counts
   - Cost data: token usage, model tier distribution, waste ratios
   - Failure data: error types, recovery success rates, stagnation events

2. **PLAN: Pattern analysis** -- Identify patterns in the data:
   - Recurring failure modes (same error type appearing across multiple tasks)
   - Cost concentrations (80/20 rule: which 20% of activities consume 80% of cost)
   - Quality bottlenecks (which stage introduces the most defects)
   - Duration outliers (tasks that take 10x longer than similar tasks)

3. **PLAN: Improvement opportunity identification** -- For each pattern, formulate a specific improvement hypothesis:
   - "If we add input validation to the prompt assembler, research task failures will decrease by 30%"
   - "If we cache LSP results during scope analysis, planning time will decrease by 50%"
   - "If we use TDD for all build tasks, Inspector rejection rate will decrease by 25%"

4. **PLAN: Prioritization** -- Rank improvements by expected value:
   - Impact: how much will this improve the target metric?
   - Effort: how much work to implement?
   - Risk: what could go wrong?
   - Priority = Impact / (Effort x Risk)

5. **DO: Change proposal** -- For the highest-priority improvement, create a concrete change proposal:
   - What to change (specific files, configurations, processes)
   - How to change it (implementation approach)
   - How to measure success (specific metrics with targets)
   - How to roll back if it makes things worse

6. **CHECK: Measurement plan** -- Define how to measure the improvement:
   - Baseline metric (current performance before the change)
   - Target metric (expected performance after the change)
   - Measurement period (how long to observe before concluding)
   - Statistical significance (how confident do we need to be)

7. **CHECK: Impact assessment** -- After the change is implemented and the measurement period has elapsed:
   - Compare actual metrics against baseline and target
   - Determine if the improvement hypothesis was confirmed
   - Calculate actual impact (percentage improvement, cost savings, time saved)
   - Identify any unintended side effects

8. **ACT: Standardize or pivot** -- Based on results:
   - If improvement confirmed: standardize the change, update documentation, add to best practices
   - If no improvement: revert the change, document what was learned
   - If partial improvement: refine the approach and start a new cycle

9. **Cycle documentation** -- Record the complete PDCA cycle for future reference:
   - Hypothesis, approach, results, lessons learned
   - This becomes input to the next improvement cycle

## Output Format

The skill produces a PDCA improvement report:

```json
{
  "success": true,
  "output": {
    "cycle_id": "PDCA-2025-01-15",
    "phase": "plan",
    "focus_area": "quality",
    "findings": [
      {
        "pattern": "40% of Inspector rejections cite missing type annotations",
        "root_cause": "Build tasks do not consistently add type annotations to new code",
        "improvement": "Add pre-submission type annotation check to build mode self-check",
        "expected_impact": "Reduce Inspector rejection rate from 22% to 13%",
        "effort": "S",
        "priority": 1
      }
    ],
    "proposed_change": {
      "description": "Add mypy --strict check to build mode self-check before submitting to Inspector",
      "files_to_modify": ["vetinari/agents/consolidated/worker_agent.py"],
      "measurement": {
        "baseline": "Inspector rejection rate: 22% (last 30 days)",
        "target": "Inspector rejection rate: <15%",
        "measurement_period": "30 days after implementation"
      },
      "rollback": "Remove mypy check from self-check, revert to previous behavior"
    },
    "prior_cycle_results": [
      {
        "cycle_id": "PDCA-2024-12-15",
        "hypothesis": "Caching prompt assembly reduces planning time",
        "result": "confirmed",
        "impact": "Planning time reduced by 35% (target was 50%)"
      }
    ]
  },
  "metadata": {
    "data_source": "episode_memory",
    "records_analyzed": 450,
    "analysis_period": "2024-12-15 to 2025-01-15"
  }
}
```

## Quality Standards

This skill is governed by the following standards from the skill registry:

- **STD-WRK-020**: Improvement mode MUST follow PDCA cycle: Plan, Do, Check, Act
- **STD-UNI-001**: All skill outputs MUST conform to the declared output_schema
- **STD-UNI-003**: All skill executions MUST log entry and exit at INFO level with timing
- **STD-UNI-004**: Skill executions SHOULD be idempotent
- **CON-WRK-009**: Operations modes run post-execution -- MUST NOT modify already-reviewed code
- **GDL-WRK-008**: Run improvement mode after every major feature completion

## Examples

### Example: Improving build task success rate

**Input:**
```
task: "Analyze why 30% of build tasks require replan and propose improvements"
metrics: {build_success_rate: 0.70, avg_retries: 1.4, top_failure: "scope_underestimate"}
focus_area: "reliability"
history: [last 200 build task results from episode_memory]
```

**Output (abbreviated):**
```
findings:
  1. "60% of build failures stem from scope underestimation -- the task description implied 2-3 files but actually required 5-8 files"
  2. "25% of failures are from missing prerequisites -- build started before research was complete"
  3. "15% are genuine implementation errors"

proposed_change:
  description: "Make scope-analysis a mandatory prerequisite for all build tasks with effort >= M"
  expected_impact: "Build success rate from 70% to 85% (scope failures reduced by 60%)"
  effort: S
  measurement:
    baseline: "Build success rate: 70%"
    target: "Build success rate: >82%"
    period: "Next 100 build tasks"
```
