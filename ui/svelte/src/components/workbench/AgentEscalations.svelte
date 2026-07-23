<script>
  /** Escalation list for Mission Control. */
  let { rows = [], projectId = null } = $props();

  function runHref(runId) {
    return projectId && runId ? `/projects/${projectId}/run-kernel?run_id=${encodeURIComponent(runId)}` : null;
  }

  function planHref(planId) {
    return projectId && planId ? `/projects/${projectId}/decomposition?plan_id=${encodeURIComponent(planId)}` : null;
  }

  function formatTimestamp(value) {
    if (!value) return 'time unknown';
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
  }
</script>

{#if rows.length === 0}
  <p class="empty">No escalations for this project.</p>
{:else}
  <div class="escalation-list">
    {#each rows as row (row.run_id + ':' + row.task_id)}
      <article
        class="escalation-card"
        role="region"
        aria-labelledby="escalation-{row.run_id}-heading"
        data-escalated="true"
        data-escalation-reason={row.escalation_reason}
      >
        <h3 id="escalation-{row.run_id}-heading">{row.agent_type} escalation</h3>
        <dl>
          <dt>Run</dt>
          <dd>
            {#if runHref(row.run_id)}
              <a href={runHref(row.run_id)}>{row.run_id}</a>
            {:else}
              {row.run_id}
            {/if}
          </dd>
          <dt>Task</dt>
          <dd>{row.task_id}</dd>
          <dt>Reason</dt>
          <dd class="reason">{row.escalation_reason}</dd>
          <dt>Escalated</dt>
          <dd>{formatTimestamp(row.escalated_at_utc)}</dd>
        </dl>
        {#if planHref(row.child_plan_id)}
          <a class="child-plan" href={planHref(row.child_plan_id)}>View child plan</a>
        {/if}
      </article>
    {/each}
  </div>
{/if}

<style>
  .empty {
    color: var(--text-muted, #94a3b8);
  }

  .escalation-list {
    display: grid;
    gap: 10px;
  }

  .escalation-card {
    border: 1px solid rgba(248, 113, 113, 0.45);
    border-radius: 8px;
    padding: 12px;
    background: rgba(127, 29, 29, 0.16);
  }

  h3 {
    margin: 0 0 8px;
    font-size: 0.95rem;
  }

  dl {
    display: grid;
    grid-template-columns: max-content minmax(0, 1fr);
    gap: 6px 10px;
    margin: 0;
  }

  dt {
    color: var(--text-muted, #94a3b8);
  }

  dd {
    margin: 0;
    min-width: 0;
  }

  .reason {
    white-space: pre-wrap;
  }

  .child-plan {
    display: inline-block;
    margin-top: 10px;
  }
</style>
