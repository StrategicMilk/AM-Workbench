# Kaizen Workflow

Kaizen is the operator-facing continuous improvement loop for AM Workbench. It
is not limited to model training: use it whenever a run, review, or support case
reveals repeated friction.

## Primary Entry Points

| Surface | Purpose |
|---|---|
| Workbench navigation: Kaizen | Review the PDCA workflow, kaizen score, and related training history from the UI. |
| `python -m vetinari kaizen report` | Print the weekly counts plus next actions. |
| `python -m vetinari kaizen gemba` | Run an on-demand friction inspection and propose improvements. |

## PDCA Loop

1. Plan: read the weekly report and identify the highest-risk confirmed,
   failed, or reverted improvements.
2. Do: run `python -m vetinari kaizen gemba` after a real workflow or support
   case to capture concrete friction.
3. Check: compare the new findings with the kaizen score, failed count, and
   reverted count before promoting changes.
4. Act: promote only changes with visible evidence and leave blocked proposals
   open with a specific next diagnostic step.

## Support Evidence

When a kaizen issue needs escalation, attach the report output and the exact
gemba finding that motivated the change. Do not attach prompt text, secrets,
raw databases, model files, or local absolute checkout paths.
