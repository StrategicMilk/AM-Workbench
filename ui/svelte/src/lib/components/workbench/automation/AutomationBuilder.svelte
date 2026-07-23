<script>
  import { AutomationTriggerSource, FailurePolicy } from '$lib/contracts/enums.js';

  const {
    automation = undefined,
    simulation = undefined,
    triggerSources = [],
    onChange = undefined,
    onDryRun = undefined,
  } = $props();

  const sourceList = $derived(
    triggerSources.length > 0
      ? triggerSources
      : [
          AutomationTriggerSource.FILE_CHANGE,
          AutomationTriggerSource.SOURCE_STALENESS,
          AutomationTriggerSource.NEW_TRACE,
          AutomationTriggerSource.FAILED_EVAL,
          AutomationTriggerSource.NEW_MODEL,
          AutomationTriggerSource.DATASET_DRIFT,
          AutomationTriggerSource.BENCHMARK_CHANGE,
          AutomationTriggerSource.COST_THRESHOLD,
          AutomationTriggerSource.ANNOTATION_QUEUE,
          AutomationTriggerSource.TRAINING_COMPLETION,
          AutomationTriggerSource.CRON,
        ],
  );

  const triggerLabels = {
    annotation_queue: 'Annotation queue',
    benchmark_change: 'Benchmark change',
    cost_threshold: 'Cost threshold',
    cron: 'Schedule',
    dataset_drift: 'Dataset drift',
    failed_eval: 'Failed evaluation',
    file_change: 'File change',
    new_model: 'New model',
    new_trace: 'New trace',
    source_staleness: 'Source staleness',
    training_completion: 'Training completion',
  };

  const blockerLabels = {
    'approval evidence missing': 'Approval evidence is required',
    'approval-evidence-missing': 'Approval evidence is required',
    'budget-cost-exceeded': 'Estimated cost is above the cap',
    'budget-runtime-exceeded': 'Estimated runtime is above the cap',
    'high-impact self-promotion blocked': 'High-impact automations cannot self-promote',
    'quiet-hours-active': 'Quiet hours are active',
    'resource-lease-unavailable': 'Required resource lease is unavailable',
  };

  function formatDisplayLabel(value) {
    return String(value ?? '')
      .trim()
      .replace(/^condition-mismatch:/, 'Condition mismatch: ')
      .replace(/[-_]+/g, ' ')
      .replace(/\s+/g, ' ')
      .replace(/\b\w/g, (char) => char.toUpperCase());
  }

  function triggerLabel(source) {
    return triggerLabels[source] ?? formatDisplayLabel(source);
  }

  function blockerLabel(reason) {
    return blockerLabels[reason] ?? formatDisplayLabel(reason);
  }

  let draft = $state(
    automation ?? {
      automation_id: '',
      name: '',
      trigger: AutomationTriggerSource.FILE_CHANGE,
      condition: { description: '', required_context: {} },
      action: { action_type: '', target_ref: '', high_impact: false, self_promote: false, parameters: {} },
      approval: { required: false, approver_role: '', evidence_ref: '' },
      rollback: { strategy: '', target_ref: '' },
      budget: { max_cost_usd: 1, max_runtime_minutes: 15 },
      quiet_hours: { timezone: 'local', start_hour: 22, end_hour: 7 },
      rate_limit: { max_runs: 1, window_minutes: 60 },
      resource_lease: { lane: 'default', resource_ref: 'workbench-default', required: true },
      failure_policy: FailurePolicy.PROPOSE_ONLY,
      enabled: true,
    },
  );

  const approvalBlocked = $derived(draft.action.high_impact && !draft.approval.evidence_ref?.trim());
  const selfPromotionBlocked = $derived(draft.action.high_impact && draft.action.self_promote);
  const blockedReasons = $derived([
    ...(approvalBlocked ? ['approval evidence missing'] : []),
    ...(selfPromotionBlocked ? ['high-impact self-promotion blocked'] : []),
    ...(simulation?.blocked_reasons ?? []),
  ]);
  const displayBlockedReasons = $derived(blockedReasons.map(blockerLabel));

  function update(path, value) {
    const next = structuredClone(draft);
    let cursor = next;
    for (const key of path.slice(0, -1)) {
      if (!cursor[key] || typeof cursor[key] !== 'object') cursor[key] = {};
      cursor = cursor[key];
    }
    cursor[path[path.length - 1]] = value;
    draft = next;
    if (typeof onChange === 'function') {
      onChange(next);
    }
  }

  function dryRun() {
    if (typeof onDryRun === 'function') {
      onDryRun(draft);
    }
  }

  const fieldIds = {
    name: 'automation-builder-name',
    trigger: 'automation-builder-trigger',
    condition: 'automation-builder-condition',
    cronExpression: 'automation-builder-cron-expression',
    action: 'automation-builder-action',
    target: 'automation-builder-target',
    costCap: 'automation-builder-cost-cap',
    runCap: 'automation-builder-run-cap',
    highImpact: 'automation-builder-high-impact',
    approvalRequired: 'automation-builder-approval-required',
    selfPromote: 'automation-builder-self-promote',
    approverRole: 'automation-builder-approver-role',
    approvalEvidence: 'automation-builder-approval-evidence',
    rollback: 'automation-builder-rollback',
    lease: 'automation-builder-lease',
  };
</script>

<section class="automation-builder" aria-label="Workbench automation builder">
  <div class="builder-grid">
    <label>
      <span>Name</span>
      <input id={fieldIds.name} value={draft.name} oninput={(event) => update(['name'], event.currentTarget.value)} />
    </label>
    <label>
      <span>Trigger</span>
      <select id={fieldIds.trigger} value={draft.trigger} onchange={(event) => update(['trigger'], event.currentTarget.value)}>
        {#each sourceList as source (source)}
          <option value={source}>{triggerLabel(source)}</option>
        {/each}
      </select>
    </label>
    <label class="wide">
      <span>Condition</span>
      <input
        id={fieldIds.condition}
        value={draft.condition.description}
        oninput={(event) => update(['condition', 'description'], event.currentTarget.value)}
      />
    </label>
    {#if draft.trigger === AutomationTriggerSource.CRON}
      <label>
        <span>Cron expression</span>
        <input
          id={fieldIds.cronExpression}
          value={draft.condition.required_context?.cron_expression ?? ''}
          placeholder="0 9 * * 1-5"
          oninput={(event) => update(['condition', 'required_context', 'cron_expression'], event.currentTarget.value)}
        />
      </label>
    {/if}
    <label>
      <span>Action</span>
      <input
        id={fieldIds.action}
        value={draft.action.action_type}
        oninput={(event) => update(['action', 'action_type'], event.currentTarget.value)}
      />
    </label>
    <label>
      <span>Target</span>
      <input
        id={fieldIds.target}
        value={draft.action.target_ref}
        oninput={(event) => update(['action', 'target_ref'], event.currentTarget.value)}
      />
    </label>
    <label>
      <span>Cost cap</span>
      <input
        id={fieldIds.costCap}
        type="number"
        min="0"
        step="0.01"
        value={draft.budget.max_cost_usd}
        oninput={(event) => update(['budget', 'max_cost_usd'], Number(event.currentTarget.value))}
      />
    </label>
    <label>
      <span>Run cap</span>
      <input
        id={fieldIds.runCap}
        type="number"
        min="1"
        value={draft.rate_limit.max_runs}
        oninput={(event) => update(['rate_limit', 'max_runs'], Number(event.currentTarget.value))}
      />
    </label>
  </div>

  <div class="toggle-row">
    <label>
      <input
        id={fieldIds.highImpact}
        type="checkbox"
        checked={draft.action.high_impact}
        onchange={(event) => update(['action', 'high_impact'], event.currentTarget.checked)}
      />
      <span>High impact</span>
    </label>
    <label>
      <input
        id={fieldIds.approvalRequired}
        type="checkbox"
        checked={draft.approval.required}
        onchange={(event) => update(['approval', 'required'], event.currentTarget.checked)}
      />
      <span>Approval required</span>
    </label>
    <label>
      <input
        id={fieldIds.selfPromote}
        type="checkbox"
        checked={draft.action.self_promote}
        onchange={(event) => update(['action', 'self_promote'], event.currentTarget.checked)}
      />
      <span>Self promote</span>
    </label>
  </div>

  <div class="builder-grid">
    <label>
      <span>Approver role</span>
      <input
        id={fieldIds.approverRole}
        value={draft.approval.approver_role}
        oninput={(event) => update(['approval', 'approver_role'], event.currentTarget.value)}
      />
    </label>
    <label>
      <span>Approval evidence</span>
      <input
        id={fieldIds.approvalEvidence}
        value={draft.approval.evidence_ref}
        oninput={(event) => update(['approval', 'evidence_ref'], event.currentTarget.value)}
      />
    </label>
    <label>
      <span>Rollback</span>
      <input
        id={fieldIds.rollback}
        value={draft.rollback.strategy}
        oninput={(event) => update(['rollback', 'strategy'], event.currentTarget.value)}
      />
    </label>
    <label>
      <span>Lease</span>
      <input
        id={fieldIds.lease}
        value={draft.resource_lease.resource_ref}
        oninput={(event) => update(['resource_lease', 'resource_ref'], event.currentTarget.value)}
      />
    </label>
  </div>

  <div class="status-row" class:blocked={blockedReasons.length > 0} role="status" aria-live="polite">
    <div>
      <strong>{blockedReasons.length > 0 ? 'Propose only' : 'Ready for dry run'}</strong>
      <span id="automation-dry-run-status">{blockedReasons.length > 0 ? displayBlockedReasons.join(', ') : 'Receipt-backed simulation available'}</span>
    </div>
    <button type="button" onclick={dryRun} disabled={blockedReasons.length > 0} aria-describedby="automation-dry-run-status">Dry run</button>
  </div>
</section>

<style>
  .automation-builder {
    display: grid;
    gap: 12px;
    width: 100%;
  }

  .builder-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
  }

  label {
    display: grid;
    gap: 4px;
    min-width: 0;
    color: var(--text-primary, #111827);
    font-size: 0.8125rem;
    font-weight: 700;
  }

  label.wide {
    grid-column: 1 / -1;
  }

  input,
  select {
    width: 100%;
    min-height: 44px;
    box-sizing: border-box;
    border: 1px solid var(--border-default, #cbd5e1);
    border-radius: 6px;
    background: var(--surface-primary, #fff);
    color: var(--text-primary, #111827);
    font: inherit;
    font-size: 0.8125rem;
  }

  .toggle-row {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    padding: 8px 0;
  }

  .toggle-row label {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .toggle-row input {
    width: 44px;
    min-height: 44px;
  }

  .status-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    min-height: 54px;
    padding: 10px;
    border: 1px solid var(--success, #15803d);
    border-radius: 6px;
    background: var(--success-muted);
  }

  .status-row.blocked {
    border-color: var(--warning, #b45309);
    background: var(--warning-muted);
  }

  .status-row div {
    display: grid;
    gap: 2px;
    min-width: 0;
  }

  .status-row strong,
  .status-row span {
    overflow-wrap: anywhere;
  }

  .status-row span {
    color: var(--text-muted, #4b5563);
    font-size: 0.75rem;
  }

  .status-row button {
    min-width: 86px;
    min-height: 44px;
    border: 1px solid var(--border-default, #cbd5e1);
    border-radius: 6px;
    background: var(--surface-secondary, #f8fafc);
    color: var(--text-primary, #111827);
    font: inherit;
    font-size: 0.8125rem;
    font-weight: 700;
    cursor: pointer;
  }

  input:focus-visible,
  select:focus-visible,
  button:focus-visible {
    border-color: var(--primary, #2563eb);
    outline: 2px solid var(--primary-soft, rgba(37, 99, 235, 0.22));
    outline-offset: 2px;
  }

  @media (max-width: 720px) {
    .builder-grid {
      grid-template-columns: 1fr;
    }

    .status-row {
      align-items: stretch;
      flex-direction: column;
    }
  }
</style>
