<script>
  /** Filterable agent-task table for Mission Control. */
  import AgentTaskRow from '$components/workbench/AgentTaskRow.svelte';

  let { rows = [], projectId = null } = $props();
  let statusFilter = $state('all');
  let laneFilter = $state('all');
  let escalatedOnly = $state(false);

  let filteredRows = $derived(
    rows.filter((row) => {
      const statusMatches = statusFilter === 'all' || row.status === statusFilter;
      const laneMatches = laneFilter === 'all' || row.lane === laneFilter;
      const escalationMatches = !escalatedOnly || row.escalated === true;
      return statusMatches && laneMatches && escalationMatches;
    })
  );
</script>

<div class="task-filters" aria-label="Agent task filters">
  <label>
    Status
    <select bind:value={statusFilter} aria-label="Filter agent tasks by status">
      <option value="all">All</option>
      <option value="queued">Queued</option>
      <option value="running">Running</option>
      <option value="succeeded">Succeeded</option>
      <option value="failed">Failed</option>
      <option value="cancelled">Cancelled</option>
      <option value="paused">Paused</option>
      <option value="blocked">Blocked</option>
    </select>
  </label>
  <label>
    Lane
    <select bind:value={laneFilter} aria-label="Filter agent tasks by lane">
      <option value="all">All</option>
      <option value="interactive">Interactive</option>
      <option value="hub_agent">Hub Agent</option>
      <option value="training">Training</option>
    </select>
  </label>
  <label class="checkbox">
    <input type="checkbox" bind:checked={escalatedOnly} aria-label="Show escalated tasks only" />
    Escalated only
  </label>
</div>

<table class="task-table" aria-label="Agent tasks">
  <thead>
    <tr>
      <th scope="col">Run ID</th>
      <th scope="col">Task ID</th>
      <th scope="col">Agent</th>
      <th scope="col">Status</th>
      <th scope="col">Lane</th>
      <th scope="col">Recursive</th>
      <th scope="col">Escalation</th>
      <th scope="col">Blocker</th>
      <th scope="col">Retries</th>
      <th scope="col">Pause</th>
      <th scope="col">Evidence</th>
    </tr>
  </thead>
  <tbody>
    {#if rows.length === 0}
      <tr>
        <td colspan="11" class="empty">No agent tasks for this project.</td>
      </tr>
    {:else if filteredRows.length === 0}
      <tr>
        <td colspan="11" class="empty">No tasks match the current filters.</td>
      </tr>
    {:else}
      {#each filteredRows as row (row.run_id + ':' + row.task_id)}
        <AgentTaskRow {row} {projectId} />
      {/each}
    {/if}
  </tbody>
</table>

<style>
  .task-filters {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    margin-bottom: 10px;
  }

  label {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    color: var(--text-muted, #94a3b8);
    font-size: var(--text-sm, 0.875rem);
  }

  select {
    border: 1px solid var(--border-color, #334155);
    border-radius: 6px;
    background: var(--surface-elevated, #111827);
    color: inherit;
    min-height: 44px;
    padding: 8px 10px;
  }

  .checkbox {
    color: var(--text-primary, #e5e7eb);
  }

  .checkbox input {
    min-height: 20px;
    width: 20px;
  }

  .task-table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--text-sm, 0.875rem);
  }

  th {
    border-bottom: 1px solid var(--border-color, #334155);
    color: var(--text-muted, #94a3b8);
    padding: 8px;
    text-align: left;
  }

  .empty {
    color: var(--text-muted, #94a3b8);
    padding: 12px;
  }
</style>
