<script>
  let { runHandle = {}, events = [] } = $props();
  let latestEvent = $derived(events.length ? events[events.length - 1] : null);
  let stepState = $derived(runHandle.step_state ?? 'planning');
  let streamLabel = $derived(runHandle.replay_status ?? 'unavailable');
</script>

<section class="run-handle-panel" aria-label="Run handle" data-testid="run-handle-panel">
  <header>
    <h3>Run Handle</h3>
    <span class:ok={runHandle.status === 'allowed' || runHandle.status === 'running'}>{runHandle.status ?? 'degraded'}</span>
  </header>

  <dl>
    <div>
      <dt>Run</dt>
      <dd>{runHandle.run_id ?? 'run.pending'}</dd>
    </div>
    <div>
      <dt>Stream</dt>
      <dd>{runHandle.stream_id ?? 'stream.pending'}</dd>
    </div>
    <div>
      <dt>Step</dt>
      <dd>{stepState}</dd>
    </div>
    <div>
      <dt>Replay</dt>
      <dd>{streamLabel}</dd>
    </div>
  </dl>

  {#if latestEvent}
    <p>{latestEvent.event_type ?? latestEvent.label} <span>{latestEvent.status ?? ''}</span></p>
  {/if}
</section>

<style>
  .run-handle-panel {
    display: grid;
    gap: 10px;
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    padding: 10px;
    background: rgba(15, 23, 42, 0.72);
  }

  header {
    display: flex;
    justify-content: space-between;
    gap: 10px;
  }

  h3 {
    margin: 0;
    font-size: 0.86rem;
  }

  header span {
    color: #f59e0b;
    font-size: 0.76rem;
  }

  header span.ok {
    color: #22c55e;
  }

  dl {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
    margin: 0;
  }

  dt {
    color: var(--text-muted, #94a3b8);
    font-size: 0.68rem;
    text-transform: uppercase;
  }

  dd {
    margin: 2px 0 0;
    color: var(--text-primary, #e5e7eb);
    font-size: 0.78rem;
    overflow-wrap: anywhere;
  }

  p {
    margin: 0;
    color: var(--text-primary, #e5e7eb);
    font-size: 0.78rem;
  }

  p span {
    color: var(--text-muted, #94a3b8);
  }
</style>
