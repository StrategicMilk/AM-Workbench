<script>
  let { events = [] } = $props();
  let safeEvents = $derived(Array.isArray(events) ? events.filter((event) => event?.event_id) : []);
</script>

<section class="timeline-panel" aria-label="Persistent timeline" data-testid="workbench-timeline-panel">
  <header>
    <h2>Timeline</h2>
    <span role="status" aria-label={`${safeEvents.length} timeline events`}>{safeEvents.length}</span>
  </header>
  <ol>
    {#each safeEvents as event (event.event_id)}
      <li class={event.severity}>
        <time datetime={event.occurred_at_utc}>{event.occurred_at_utc}</time>
        <strong>{event.label}</strong>
        <span>{event.object_kind}: {event.object_id}</span>
        {#if event.severity}
          <span class="severity-label">{event.severity}</span>
        {/if}
      </li>
    {:else}
      <li class="empty" role="status">No timeline events.</li>
    {/each}
  </ol>
</section>

<style>
  .timeline-panel {
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    background: var(--surface-elevated, #111827);
    min-width: 0;
  }

  header {
    display: flex;
    justify-content: space-between;
    gap: 10px;
    border-bottom: 1px solid var(--border-default, #334155);
    padding: 10px 12px;
  }

  h2 {
    margin: 0;
    font-size: 0.92rem;
  }

  header span,
  li span,
  time {
    color: var(--text-muted, #94a3b8);
    font-size: 0.76rem;
  }

  .severity-label {
    font-weight: 700;
    text-transform: capitalize;
  }

  ol {
    display: grid;
    gap: 0;
    margin: 0;
    max-height: 360px;
    overflow: auto;
    padding: 8px;
    list-style: none;
  }

  li {
    display: grid;
    gap: 3px;
    border-left: 2px solid #38bdf8;
    padding: 7px 0 8px 10px;
  }

  li.warning {
    border-color: #f59e0b;
  }

  li.error {
    border-color: #ef4444;
  }

  li.empty {
    border-color: var(--border-default, #334155);
    color: var(--text-muted, #94a3b8);
  }

  strong {
    color: var(--text-primary, #e5e7eb);
    font-size: 0.84rem;
    overflow-wrap: anywhere;
  }
</style>
