<script>
  let { leases = [], safeActions = [], onAction = () => {} } = $props();

  function actionsFor(lease) {
    return safeActions.filter((action) => action.target_ref === lease.lease_id);
  }

  function shortId(value) {
    return value ? `${value}`.slice(0, 10) : 'unknown';
  }
</script>

<section class="lease-table" aria-label="Active resource leases">
  <header>
    <h2>Active Leases</h2>
    <span>{leases.length}</span>
  </header>

  <div class="table" role="table">
    <div class="row head" role="row">
      <span role="columnheader">Lease</span>
      <span role="columnheader">Workload</span>
      <span role="columnheader">Model</span>
      <span role="columnheader">Lane</span>
      <span role="columnheader">Placement</span>
      <span role="columnheader">Status</span>
      <span role="columnheader">Actions</span>
    </div>
    {#each leases as lease (lease.lease_id)}
      <div class="row" role="row">
        <span role="cell">{shortId(lease.lease_id)}</span>
        <span role="cell">{lease.workload_kind ?? 'unknown'}</span>
        <span role="cell">{lease.model_id ?? 'unknown'}</span>
        <span role="cell">{lease.lane ?? 'unknown'}</span>
        <span role="cell">{lease.placement ?? 'unknown'}</span>
        <span role="cell" class="pill" data-status={lease.status}>{lease.status ?? 'unknown'}</span>
        <span role="cell" class="actions">
          {#each actionsFor(lease) as action (action.action_id)}
            {#if action.action_id !== 'cancel' || lease.status === 'approved'}
              <button type="button" onclick={() => onAction(action)} disabled={action.action_id === 'cancel' && lease.status !== 'approved'}>
                {action.label}
              </button>
            {/if}
          {/each}
        </span>
      </div>
      {#if lease.reasons?.length}
        <div class="reason-row" role="row">
          <span role="cell">Reasons</span>
          <span role="cell" class="reason-cell">{lease.reasons.join(', ')}</span>
        </div>
      {/if}
    {/each}
  </div>
</section>

<style>
  .lease-table {
    display: grid;
    gap: 12px;
  }

  header,
  .row,
  .reason-row {
    display: grid;
    gap: 10px;
    align-items: center;
  }

  header {
    grid-template-columns: 1fr auto;
  }

  h2 {
    margin: 0;
    font-size: 16px;
    letter-spacing: 0;
  }

  .table {
    display: grid;
    gap: 6px;
    overflow-x: auto;
  }

  .row {
    grid-template-columns: 90px 120px 140px 120px 110px 110px minmax(180px, 1fr);
    min-width: 900px;
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    background: var(--surface-elevated, #111827);
    padding: 9px;
  }

  .head {
    color: var(--text-muted, #94a3b8);
    font-size: 12px;
  }

  .reason-row {
    grid-template-columns: 90px 1fr;
    min-width: 900px;
    color: var(--text-muted, #94a3b8);
    font-size: 12px;
    padding: 0 9px 5px;
  }

  .reason-cell {
    overflow-wrap: anywhere;
  }

  .pill {
    color: #fbbf24;
  }

  .pill[data-status="approved"] {
    color: #86efac;
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  button {
    border: 1px solid var(--border-default, #334155);
    border-radius: 6px;
    background: var(--surface-panel, #0f172a);
    color: inherit;
    padding: 5px 8px;
    font-size: 12px;
  }
</style>
