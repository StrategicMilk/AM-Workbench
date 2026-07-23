<script>
  import Icon from '$lib/a11y/Icon.svelte';
  import * as fmt from '$lib/utils/format.js';

  let {
    sessions = [],
    stats = null,
    selectedSession = '',
    onSelectSession,
  } = $props();
</script>

<aside class="memory-sidebar" aria-label="Memory statistics">
  <section class="card stats-card">
    <h3 class="sidebar-section-title"><Icon name="chart-bar" /> Stats</h3>
    {#if stats}
      <dl class="stats-list">
        <dt>Total entries</dt>
        <dd>{fmt.integer(stats.total_entries ?? 0)}</dd>
        <dt>Sessions</dt>
        <dd>{fmt.integer(stats.total_sessions ?? sessions.length)}</dd>
        <dt>Searches run</dt>
        <dd>{fmt.integer(stats.search_count ?? 0)}</dd>
        <dt>Oldest entry</dt>
        <dd>{fmt.relativeTime(stats.oldest_entry)}</dd>
        <dt>Latest entry</dt>
        <dd>{fmt.relativeTime(stats.latest_entry)}</dd>
      </dl>
    {:else}
      <p class="text-muted">Stats unavailable.</p>
    {/if}
  </section>

  <section class="card sessions-card">
    <h3 class="sidebar-section-title"><Icon name="layer-group" /> Sessions</h3>
    {#if sessions.length === 0}
      <p class="text-muted">No sessions recorded.</p>
    {:else}
      <ul class="session-list" aria-label="Memory sessions">
        {#each sessions as session (session.id ?? session)}
          {@const sid = session.id ?? session}
          <li>
            <button
              class="session-btn"
              class:active={selectedSession === sid}
              onclick={() => onSelectSession(selectedSession === sid ? '' : sid)}
              aria-pressed={selectedSession === sid}
              aria-label="Filter by session {sid}"
            >
              <span class="session-id">{session.label ?? sid}</span>
              {#if session.count != null}
                <span class="session-count">{session.count}</span>
              {/if}
            </button>
          </li>
        {/each}
      </ul>
    {/if}
  </section>
</aside>

<style>
  .memory-sidebar {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .card {
    background: var(--surface-elevated);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-lg);
    padding: 16px;
  }

  .sidebar-section-title {
    font-size: 0.875rem;
    font-weight: 600;
    color: var(--text-primary);
    margin: 0 0 12px 0;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .sidebar-section-title i { color: var(--text-muted); }

  .stats-list {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 6px 8px;
    margin: 0;
    font-size: 0.8125rem;
  }

  .stats-list dt { color: var(--text-muted); }
  .stats-list dd { color: var(--text-primary); font-weight: 500; margin: 0; text-align: right; }

  .session-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .session-btn {
    width: 100%;
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: none;
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    min-height: 44px;
    padding: 6px 10px;
    cursor: pointer;
    font-size: 0.8125rem;
    font-family: inherit;
    color: var(--text-secondary);
    transition: background var(--transition-base), color var(--transition-base);
    text-align: left;
  }

  .session-btn:hover { background: var(--surface-hover); color: var(--text-primary); }
  .session-btn.active { background: var(--primary-muted); color: var(--primary); border-color: var(--primary); }

  .session-id {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .session-count {
    font-size: 0.75rem;
    color: var(--text-muted);
    flex-shrink: 0;
  }

  .text-muted {
    color: var(--text-muted);
    font-size: 0.875rem;
  }
</style>
