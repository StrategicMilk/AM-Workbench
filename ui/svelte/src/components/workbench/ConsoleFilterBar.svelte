<script>
  import { RunKind, TaskStatus } from '$lib/contracts/enums.js';

  let { kind = null, status = null, leaseId = null, onChange = () => {} } = $props();

  const RUN_KIND_OPTIONS = [null, RunKind.AGENT_RUN, RunKind.TRAINING_RUN, RunKind.EVAL_RUN, RunKind.GATEWAY_REQUEST];
  const RUN_STATUS_OPTIONS = [null, TaskStatus.RUNNING, TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.BLOCKED];

  function emit(next = {}) {
    const nextKind = Object.hasOwn(next, 'kind') ? next.kind : kind;
    const nextStatus = Object.hasOwn(next, 'status') ? next.status : status;
    const nextLeaseId = Object.hasOwn(next, 'leaseId') ? next.leaseId : leaseId;
    onChange({ kind: nextKind, status: nextStatus, leaseId: nextLeaseId || null });
  }
</script>

<div class="filter-bar" aria-label="Workbench console filters">
  <div class="chip-group" role="group" aria-label="Run kind">
    {#each RUN_KIND_OPTIONS as value (value ?? 'all-kind')}
      <button
        class:active={kind === value}
        type="button"
        aria-pressed={kind === value}
        onclick={() => emit({ kind: value })}
      >
        {value ?? 'all kinds'}
      </button>
    {/each}
  </div>
  <div class="chip-group" role="group" aria-label="Run status">
    {#each RUN_STATUS_OPTIONS as value (value ?? 'all-status')}
      <button
        class:active={status === value}
        type="button"
        aria-pressed={status === value}
        onclick={() => emit({ status: value })}
      >
        {value ?? 'all status'}
      </button>
    {/each}
  </div>
  <input value={leaseId ?? ''} oninput={(event) => emit({ leaseId: event.target.value })} aria-label="Lease id filter" />
  <button type="button" aria-label="Clear console filters" onclick={() => emit({ kind: null, status: null, leaseId: '' })}>clear</button>
</div>

<style>
  .filter-bar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .chip-group { display: flex; gap: 4px; flex-wrap: wrap; }
  button, input { border: 1px solid var(--border-default); border-radius: 6px; background: var(--surface-elevated); color: var(--text-secondary); font: inherit; font-size: 0.78rem; padding: 6px 8px; }
  button { cursor: pointer; }
  button.active { background: var(--primary-muted); color: var(--primary); border-color: var(--primary); }
  input { width: 120px; }
</style>
