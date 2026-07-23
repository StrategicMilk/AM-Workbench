<script>
  let { queued = [] } = $props();

  const buckets = new Set(['under-1m', '1-5m', '5-15m', 'over-15m', 'unknown']);

  function waitLabel(job) {
    return buckets.has(job.expected_wait_label) ? job.expected_wait_label : 'unknown';
  }
</script>

<section class="queued-table" aria-label="Queued resource jobs">
  <header>
    <h2>Queued Jobs</h2>
    <span>{queued.length}</span>
  </header>

  <div class="jobs">
    {#each queued as job (job.workload_id)}
      <article>
        <div>
          <strong>{job.workload_kind ?? 'unknown'}</strong>
          <span>{job.lane ?? 'unknown'}</span>
        </div>
        <dl>
          <div><dt>wait</dt><dd>{waitLabel(job)}</dd></div>
          <div><dt>action</dt><dd>{job.over_cap_action ?? 'unknown'}</dd></div>
          <div><dt>reason</dt><dd>{job.reason ?? 'queue-pressure'}</dd></div>
        </dl>
      </article>
    {:else}
      <p class="empty">No queued jobs reported.</p>
    {/each}
  </div>
</section>

<style>
  .queued-table,
  .jobs {
    display: grid;
    gap: 12px;
  }

  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }

  h2 {
    margin: 0;
    font-size: 16px;
    letter-spacing: 0;
  }

  article,
  .empty {
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    background: var(--surface-elevated, #111827);
    padding: 10px;
  }

  article > div,
  dl div {
    display: flex;
    justify-content: space-between;
    gap: 12px;
  }

  dl {
    display: grid;
    gap: 6px;
    margin: 10px 0 0;
  }

  dt,
  .empty,
  article span {
    color: var(--text-muted, #94a3b8);
  }

  dd {
    margin: 0;
    text-align: right;
  }
</style>
