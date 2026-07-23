<script>
  /**
   * Attention track — top-of-page band that surfaces every project blocked
   * on user input. Reads only real WorkReceipts (no synthetic placeholders);
   * each row shows the structured ``awaiting_reason`` populated by Foreman
   * or Inspector at the time the block was raised.
   *
   * Pulls from /api/attention on mount and on a 30 s timer. The empty state
   * shows "No attention required" rather than a fake status, satisfying the
   * "no synthetic placeholders" requirement of the Control Center.
   */
  import * as api from '$lib/api.js';
  import * as fmt from '$lib/utils/format.js';

  let items = $state([]);
  let loading = $state(true);
  let error = $state(null);
  let lastFetched = $state(null);

  async function fetchAttention() {
    try {
      const data = await api.listAttention();
      items = data?.items ?? [];
      error = null;
      lastFetched = new Date();
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    fetchAttention();
    const interval = setInterval(fetchAttention, 30_000);
    return () => clearInterval(interval);
  });
</script>

<section class="attention-track" aria-label="Attention required">
  <header class="attention-header">
    <h3>
      <i class="fas fa-bell" aria-hidden="true"></i>
      Attention required
      {#if items.length > 0}
        <span class="badge" aria-label="{items.length} attention item{items.length === 1 ? '' : 's'}">{items.length}</span>
      {/if}
    </h3>
    {#if lastFetched}
      <span class="muted" aria-hidden="true">updated {fmt.relativeTime ? fmt.relativeTime(lastFetched) : lastFetched.toLocaleTimeString()}</span>
    {/if}
  </header>

  {#if error}
    <p class="attention-error" role="alert">
      Could not load attention list: {error}
    </p>
  {:else if loading}
    <p class="muted" role="status">Loading...</p>
  {:else if items.length === 0}
    <p class="muted empty-state" role="status">No attention required</p>
  {:else}
    <ul class="attention-list">
      {#each items as item (item.receipt_id)}
        <li class="attention-row">
          <div class="attention-meta">
            <span class="project">{item.project_id}</span>
            <span class="kind kind-{item.kind}">{item.kind}</span>
          </div>
          <p class="reason">{item.awaiting_reason ?? 'awaiting user'}</p>
          <time class="when" datetime={item.finished_at_utc}>{item.finished_at_utc}</time>
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .attention-track {
    background: var(--surface-bg);
    border: 1px solid var(--border-default);
    border-radius: 12px;
    padding: 16px 20px;
    margin-bottom: 16px;
  }

  .attention-header {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 8px;
  }

  .attention-header h3 {
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 0;
    font-size: 1rem;
  }

  .badge {
    background: var(--warning, #d99404);
    color: #000;
    font-size: 0.75rem;
    border-radius: 999px;
    padding: 2px 10px;
    font-weight: 600;
  }

  .muted {
    color: var(--text-muted);
    font-size: 0.8125rem;
  }

  .empty-state {
    margin: 0;
    padding: 8px 0;
  }

  .attention-error {
    color: var(--danger);
    margin: 0;
  }

  .attention-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .attention-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    grid-template-rows: auto auto;
    gap: 4px 12px;
    padding: 10px 12px;
    background: rgba(255, 255, 255, 0.03);
    border-radius: 8px;
    border-left: 3px solid var(--warning, #d99404);
  }

  .attention-meta {
    display: flex;
    align-items: center;
    gap: 8px;
    grid-column: 1 / 2;
  }

  .project {
    font-weight: 600;
    color: var(--text-primary);
  }

  .kind {
    font-size: 0.75rem;
    color: var(--text-muted);
    border: 1px solid var(--border-default);
    border-radius: 4px;
    padding: 1px 6px;
  }

  .reason {
    grid-column: 1 / 2;
    grid-row: 2 / 3;
    margin: 0;
    color: var(--text-primary);
    font-size: 0.875rem;
  }

  .when {
    grid-column: 2 / 3;
    grid-row: 1 / 3;
    align-self: center;
    color: var(--text-muted);
    font-size: 0.75rem;
    font-variant-numeric: tabular-nums;
  }
</style>
