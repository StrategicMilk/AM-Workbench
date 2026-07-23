<script>
  /** Lease table for Mission Control queue state. */
  let { entries = [], projectId = null } = $props();

  function formatAge(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds ?? 0)));
    if (total < 60) return `${total}s`;
    const minutes = Math.floor(total / 60);
    if (minutes < 60) return `${minutes}m ${total % 60}s`;
    return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
  }

  function runHref(runId) {
    return projectId && runId ? `/projects/${projectId}/runs/${runId}` : null;
  }
</script>

<table class="lease-table" aria-label="Queue and lease rows">
  <thead>
    <tr>
      <th scope="col">Lane</th>
      <th scope="col">Caller Subsystem</th>
      <th scope="col">Lease ID</th>
      <th scope="col">Run ID</th>
      <th scope="col">State</th>
      <th scope="col">Age</th>
    </tr>
  </thead>
  {#if entries.length === 0}
    <tbody data-empty="true">
      <tr>
        <td colspan="6" class="empty">No active leases for this project.</td>
      </tr>
    </tbody>
  {:else}
    <tbody>
      {#each entries as entry (entry.lease_id)}
        <tr data-state={entry.state} data-target={entry.target}>
          <td>{entry.target}</td>
          <td>{entry.caller_subsystem}</td>
          <td><code>{entry.lease_id}</code></td>
          <td>
            {#if runHref(entry.run_id)}
              <a href={runHref(entry.run_id)}>{entry.run_id}</a>
            {:else if entry.run_id}
              <span>{entry.run_id}</span>
            {:else}
              <span class="muted">none</span>
            {/if}
          </td>
          <td><span class="state-chip state-{entry.state}" role="status" aria-label={`Lease state ${entry.state}`}>{entry.state}</span></td>
          <td>{formatAge(entry.age_seconds)}</td>
        </tr>
      {/each}
    </tbody>
  {/if}
</table>

<style>
  .lease-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.85rem;
  }

  th,
  td {
    border-bottom: 1px solid var(--border-color, #334155);
    padding: 8px;
    text-align: left;
    vertical-align: top;
  }

  th {
    color: var(--text-muted, #94a3b8);
    font-weight: 700;
  }

  code {
    white-space: nowrap;
  }

  .state-chip {
    border-radius: 999px;
    padding: 2px 8px;
    font-size: 0.75rem;
    font-weight: 700;
  }

  .state-pending {
    background: var(--warning-muted);
    color: var(--warning);
  }

  .state-active {
    background: var(--success-muted);
    color: var(--success);
  }

  .state-released {
    background: var(--info-muted, var(--surface-hover));
    color: var(--info, var(--text-primary));
  }

  .state-rejected {
    background: var(--danger-muted);
    color: var(--danger);
  }

  .empty,
  .muted {
    color: var(--text-muted, #94a3b8);
  }
</style>
