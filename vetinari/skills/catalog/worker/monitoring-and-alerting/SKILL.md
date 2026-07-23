---
name: Monitoring and Alerting
description: SPC-based anomaly detection using control charts, process capability metrics, and adaptive alert thresholds
mode: monitor
agent: worker
version: "1.0.0"
capabilities:
  - monitoring
tags:
  - operations
  - monitoring
  - spc
  - alerting
  - anomaly-detection
---

# Monitoring and Alerting

## Purpose

Monitoring and Alerting applies Statistical Process Control (SPC) techniques to detect anomalies in Vetinari's operational metrics. Rather than using static thresholds ("alert if latency > 5s"), it builds control charts from historical data that adapt to the system's natural variation, distinguishing between normal fluctuation (common cause variation) and genuine problems (special cause variation). This prevents both alert fatigue (too many false alarms from tight thresholds) and missed incidents (too few alerts from loose thresholds).

## When to Use

- Continuously during system operation to detect anomalies in real-time
- When setting up monitoring for new metrics or subsystems
- When tuning alert thresholds to reduce false positives
- When investigating whether an observed change is a genuine trend or random noise
- After deploying a change, to verify it has not degraded performance
- When the continuous improvement skill needs baseline metrics with variance data

## Inputs

| Parameter       | Type            | Required | Description                                                        |
|-----------------|-----------------|----------|--------------------------------------------------------------------|
| task            | string          | Yes      | What to monitor and the monitoring objective                       |
| metric_data     | list[float]     | Yes      | Time series of metric observations                                 |
| metric_name     | string          | No       | Name of the metric being monitored                                 |
| baseline_data   | list[float]     | Yes      | Historical baseline for control chart calculation                  |
| alert_config    | dict            | No       | Alert configuration (channels, severity levels, cooldown)          |
| context         | dict            | No       | System context, known events, maintenance windows                 |

## Process Steps

1. **Metric collection** -- Require caller-provided `metric_data` observations and `baseline_data`; do not compute SPC limits from descriptions alone. If either series is missing or has fewer than 20 numeric points, return `success=false` with an insufficient-data reason. Sources include:
   - Task execution durations (per mode, per agent)
   - Quality scores (Inspector pass rates, review scores)
   - Token usage and cost per task
   - Error rates and retry counts
   - Stagnation event frequency

2. **Baseline establishment** -- From historical data, compute the baseline statistics:
   - Mean (X-bar): average value under normal conditions
   - Standard deviation (sigma): typical variation
   - Require minimum 20 data points for reliable baseline
   - Exclude known anomalies (outages, experiments) from baseline calculation

3. **Control chart construction** -- Build control charts with three zones:
   - **Center line (CL)**: mean of the baseline data
   - **Upper control limit (UCL)**: CL + 3*sigma (99.7% of normal data falls below)
   - **Lower control limit (LCL)**: CL - 3*sigma (99.7% of normal data falls above)
   - Use appropriate chart type: X-bar for continuous data, p-chart for proportions, c-chart for counts

4. **Process capability analysis** -- Calculate capability indices:
   - **Cp**: process capability (how capable relative to spec limits)
   - **Cpk**: process capability adjusted for centering
   - **Pp/Ppk**: process performance (using overall variation)
   - A Cpk > 1.33 indicates the process is well-controlled

5. **Anomaly detection** -- Apply Western Electric rules to detect anomalies:
   - Rule 1: single point outside 3-sigma limits (UCL/LCL)
   - Rule 2: 8 consecutive points on the same side of the center line
   - Rule 3: 6 consecutive points steadily increasing or decreasing
   - Rule 4: 2 out of 3 consecutive points in the 2-3 sigma zone (same side)
   Each rule violation indicates a different type of process change.

6. **Trend analysis** -- Detect slow drifts that individual points might not flag:
   - Moving average calculation (window size based on data frequency)
   - Linear regression on recent data to detect upward/downward trends
   - Seasonal pattern detection (if metrics have daily/weekly cycles)
   - Change point detection using CUSUM or PELT algorithms

7. **Alert generation** -- When anomaly is detected, generate an alert with:
   - **Severity**: critical (Rule 1 + above UCL), warning (Rule 2-4), info (trend detected)
   - **Context**: current value, expected range, how far outside normal
   - **Duration**: how long the anomaly has persisted
   - **Possible causes**: recent changes, known events, correlated metrics

8. **Alert deduplication and cooldown** -- Prevent alert storms:
   - Deduplicate: do not alert on the same anomaly more than once per cooldown period
   - Cooldown: wait N minutes between alerts for the same metric
   - Escalation: if an anomaly persists for >N minutes, escalate severity
   - Resolution: auto-resolve alert when metric returns to normal range

9. **Dashboard summary** -- Produce a monitoring dashboard showing:
   - Current status of all monitored metrics (green/yellow/red)
   - Control charts with recent data points
   - Active alerts and their durations
   - Process capability scores

## Output Format

The skill produces a monitoring report:

```json
{
  "success": true,
  "output": {
    "metric": "task_completion_duration_seconds",
    "status": "warning",
    "control_chart": {
      "cl": 45.2,
      "ucl": 78.5,
      "lcl": 11.9,
      "sigma": 11.1,
      "latest_value": 72.3,
      "zone": "2-3 sigma (warning zone)"
    },
    "capability": {
      "cpk": 1.15,
      "interpretation": "Marginally capable -- approaching control limits"
    },
    "anomalies": [
      {
        "rule": "Rule 4 (2 of 3 points in warning zone)",
        "severity": "warning",
        "first_detected": "2025-01-15T14:30:00Z",
        "duration_minutes": 45,
        "values": [68.1, 72.3],
        "possible_causes": [
          "Model API latency increase detected at 14:15",
          "3 concurrent plans executing (normal max: 2)"
        ]
      }
    ],
    "trend": {
      "direction": "increasing",
      "slope": "+2.1 seconds per day",
      "significant": false,
      "note": "Slight upward trend but within normal variation"
    },
    "active_alerts": 1,
    "dashboard_url": null
  },
  "metadata": {
    "data_points_analyzed": 150,
    "baseline_period": "2024-12-15 to 2025-01-14",
    "monitoring_period": "2025-01-15"
  }
}
```

## Quality Standards

This skill is governed by the following standards from the skill registry:

- **STD-UNI-001**: All skill outputs MUST conform to the declared output_schema
- **STD-UNI-003**: All skill executions MUST log entry and exit at INFO level with timing
- **STD-UNI-004**: Skill executions SHOULD be idempotent
- **CON-WRK-009**: Operations modes run post-execution -- MUST NOT modify already-reviewed code
- **CON-UNI-002**: All skills must respect their max_tokens and timeout_seconds limits

## Examples

### Example: Setting up quality monitoring

**Input:**
```
task: "Set up SPC monitoring for Inspector pass rate to detect quality degradation"
metric_name: "inspector_pass_rate"
baseline_data: [0.82, 0.85, 0.78, 0.80, 0.83, 0.81, 0.79, 0.84, 0.82, 0.80, 0.85, 0.78, 0.83, 0.81, 0.82, 0.80, 0.84, 0.79, 0.82, 0.83]
```

**Output (abbreviated):**
```
control_chart:
  cl: 0.815 (mean pass rate: 81.5%)
  ucl: 0.875 (upper limit: 87.5%)
  lcl: 0.755 (lower limit: 75.5%)
  sigma: 0.020

capability:
  cpk: 1.5 (well-controlled process)

alert_rules:
  critical: "Pass rate drops below 0.755 (LCL) -- possible systemic quality issue"
  warning: "2 of 3 observations below 0.775 -- early degradation signal"
  info: "6 consecutive declining observations -- trend alert"

recommendation: "Process is well-controlled (Cpk=1.5). Set up automated monitoring with these control limits. Review baseline monthly as the system evolves."
```
