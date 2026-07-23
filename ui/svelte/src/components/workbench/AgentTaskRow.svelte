<script>
  /** Presentational row for one Mission Control agent task. */
  let { row, projectId = null } = $props();

  function runHref(runId) {
    return projectId && runId ? `/projects/${projectId}/run-kernel?run_id=${encodeURIComponent(runId)}` : null;
  }
</script>

<tr data-status={row.status} data-escalated={row.escalated ? 'true' : 'false'}>
  <td>
    {#if runHref(row.run_id)}
      <a href={runHref(row.run_id)}>{row.run_id}</a>
    {:else}
      <span>{row.run_id}</span>
    {/if}
  </td>
  <td><code>{row.task_id}</code></td>
  <td>{row.agent_type}</td>
  <td><span class="status-chip status-{row.status}">{row.status}</span></td>
  <td>
    {#if row.lane}
      <span class="lane-chip">{row.lane}</span>
    {:else}
      <span class="muted">none</span>
    {/if}
  </td>
  <td>
    {#if row.recursive_parent_run_id}
      {#if runHref(row.recursive_parent_run_id)}
        <a class="icon-link" href={runHref(row.recursive_parent_run_id)} title="Parent run {row.recursive_parent_run_id}">↳</a>
      {:else}
        <span title="Parent run {row.recursive_parent_run_id}">↳</span>
      {/if}
    {:else}
      <span class="muted">none</span>
    {/if}
  </td>
  <td>
    {#if row.escalated}
      <span class="escalation-badge" role="alert" data-escalation-reason={row.escalation_reason ?? ''}>
        Escalated: {row.escalation_reason || 'reason missing'}
      </span>
    {:else}
      <span class="muted">none</span>
    {/if}
  </td>
  <td>
    {#if row.blocker_summary}
      <span class="blocker-pill">{row.blocker_summary}</span>
    {:else}
      <span class="muted">none</span>
    {/if}
  </td>
  <td>{row.retries > 0 ? row.retries : '0'}</td>
  <td>{row.paused ? 'Paused' : 'Running'}</td>
  <td>
    {#if row.evidence_links?.length}
      {#each row.evidence_links as link}
        <a class="evidence-link" href={link}>{link}</a>
      {/each}
    {:else}
      <span class="muted">none</span>
    {/if}
  </td>
</tr>

<style>
  td {
    border-bottom: 1px solid var(--border-color, #334155);
    padding: 8px;
    vertical-align: top;
  }

  code {
    white-space: nowrap;
  }

  .status-chip,
  .lane-chip,
  .blocker-pill,
  .escalation-badge {
    display: inline-flex;
    max-width: 100%;
    border-radius: 999px;
    padding: 2px 8px;
    font-size: 0.75rem;
    font-weight: 700;
  }

  .status-running,
  .status-queued {
    background: #1e3a8a;
    color: #dbeafe;
  }

  .status-succeeded {
    background: #064e3b;
    color: #d1fae5;
  }

  .status-failed,
  .status-cancelled,
  .blocker-pill,
  .escalation-badge {
    background: #7f1d1d;
    color: #fee2e2;
  }

  .status-paused,
  .lane-chip {
    background: #78350f;
    color: #fef3c7;
  }

  .muted {
    color: var(--text-muted, #94a3b8);
  }

  .evidence-link {
    display: block;
  }
</style>
