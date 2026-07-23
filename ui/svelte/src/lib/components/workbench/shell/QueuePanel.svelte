<script>
  let { queue = null } = $props();

  const pressureLabel = $derived(queue?.lane_pressure || 'unknown');
</script>

<section class="queue-panel" aria-label="Queue visibility" data-testid="workbench-queue-panel">
  <header>
    <h2>Queue</h2>
    <strong class:warning={pressureLabel !== 'normal' && pressureLabel !== 'unknown'}>{pressureLabel}</strong>
  </header>
  <div class="queue-counts">
    <span><b>{queue?.active_count ?? 0}</b> active</span>
    <span><b>{queue?.queued_count ?? 0}</b> queued</span>
    <span><b>{queue?.blocked_count ?? 0}</b> blocked</span>
  </div>
  <p>{queue?.why || 'Queue state is not loaded.'}</p>
</section>

<style>
  .queue-panel {
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    background: var(--surface-elevated, #111827);
    padding: 12px;
  }

  header,
  .queue-counts {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }

  h2,
  p {
    margin: 0;
  }

  h2 {
    font-size: 0.92rem;
  }

  strong {
    color: var(--text-secondary, #a8b4c4);
    font-size: var(--text-sm, 0.875rem);
  }

  strong.warning {
    color: var(--warning, #f5a524);
  }

  .queue-counts {
    margin: 14px 0 10px;
  }

  span {
    color: var(--text-muted, #94a3b8);
    font-size: var(--text-sm, 0.875rem);
  }

  b {
    color: var(--text-primary, #e5e7eb);
    display: block;
    font-size: 1.05rem;
  }

  p {
    color: var(--text-muted, #94a3b8);
    font-size: var(--text-sm, 0.875rem);
  }
</style>
