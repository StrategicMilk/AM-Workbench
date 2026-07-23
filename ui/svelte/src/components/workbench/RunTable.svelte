<script>
  import RunStatusBadge from './RunStatusBadge.svelte';

  let { runs = [], selectedRunId = null, onSelect = () => {} } = $props();
  let sortKey = $state('started_at_utc');
  let sortDir = $state('desc');

  let sortedRuns = $derived([...runs].sort((a, b) => {
    const av = a?.[sortKey] ?? '';
    const bv = b?.[sortKey] ?? '';
    const cmp = String(av).localeCompare(String(bv), undefined, { numeric: true });
    return sortDir === 'asc' ? cmp : -cmp;
  }));

  function setSort(key) {
    if (sortKey === key) {
      sortDir = sortDir === 'asc' ? 'desc' : 'asc';
      return;
    }
    sortKey = key;
    sortDir = key === 'started_at_utc' ? 'desc' : 'asc';
  }
</script>

<div class="run-table-wrap" aria-label="Workbench runs">
  <table class="run-table">
    <thead>
      <tr>
        {#each [
          ['run_id', 'Run'],
          ['kind', 'Kind'],
          ['status', 'Status'],
          ['started_at_utc', 'Started'],
          ['actor_agent_type', 'Actor'],
        ] as [key, label] (key)}
          <th aria-sort={sortKey === key ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}>
            <button
              type="button"
              onclick={() => setSort(key)}
              aria-label={`Sort by ${label}${sortKey === key ? `, currently ${sortDir === 'asc' ? 'ascending' : 'descending'}` : ''}`}
            >
              {label}
              {#if sortKey === key}
                <span aria-hidden="true">{sortDir === 'asc' ? 'up' : 'down'}</span>
              {/if}
            </button>
          </th>
        {/each}
      </tr>
    </thead>
    <tbody>
      {#each sortedRuns as run (run.run_id)}
        <tr
          class:selected={selectedRunId === run.run_id}
          data-testid="run-row-{run.run_id}"
        >
          <td class="mono">
            <button
              type="button"
              class="run-select-button"
              onclick={() => onSelect(run.run_id)}
              aria-pressed={selectedRunId === run.run_id}
              aria-label={`Select run ${run.run_id}`}
            >
              {run.run_id}
            </button>
          </td>
          <td>{run.kind}</td>
          <td><RunStatusBadge status={run.status} /></td>
          <td>{run.started_at_utc}</td>
          <td>{run.actor_agent_type}</td>
        </tr>
      {:else}
        <tr>
          <td colspan="5" class="empty">No run records returned by the spine.</td>
        </tr>
      {/each}
    </tbody>
  </table>
</div>

<style>
  .run-table-wrap { overflow: auto; border: 1px solid var(--border-default); border-radius: 8px; }
  .run-table { width: 100%; border-collapse: collapse; min-width: 720px; background: var(--surface-elevated); }
  th, td { padding: 9px 10px; border-bottom: 1px solid var(--border-subtle); text-align: left; font-size: 0.82rem; }
  th { background: var(--surface-bg); color: var(--text-muted); font-weight: 700; }
  th button { border: 0; background: transparent; color: inherit; font: inherit; cursor: pointer; display: inline-flex; gap: 6px; }
  tbody tr:hover, tbody tr.selected { background: var(--surface-hover); }
  .mono { font-family: var(--font-mono); color: var(--text-primary); }
  .run-select-button {
    border: 0;
    background: transparent;
    color: inherit;
    cursor: pointer;
    font: inherit;
    font-family: var(--font-mono);
    padding: 0;
    text-align: left;
  }
  .run-select-button:focus-visible {
    border-radius: 4px;
    outline: 2px solid var(--primary);
    outline-offset: 2px;
  }
  .empty { color: var(--text-muted); text-align: center; padding: 24px; }
</style>
