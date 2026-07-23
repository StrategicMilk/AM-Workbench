<script>
  let { status = null, isLoading = false } = $props();
  let mode = $derived(status?.mode ?? 'desktop_default');
  let readyLabel = $derived(status?.is_ready ? 'Ready' : 'Blocked');
</script>

<section class="status-card" data-testid="launcher-status-card" aria-label="Launcher status">
  <div>
    <span class="eyebrow">Launcher</span>
    <h2>{readyLabel}</h2>
    <p>{mode}</p>
  </div>
  <div class="status-pill" class:ready={status?.is_ready === true} role="status" aria-label={`Launcher status ${isLoading ? 'Checking' : readyLabel}`}>
    {isLoading ? 'Checking' : readyLabel}
  </div>
</section>

<style>
  .status-card {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    padding: 14px;
    background: var(--surface-elevated, #111827);
  }

  h2,
  p {
    margin: 0;
  }

  h2 {
    font-size: 1.1rem;
  }

  .eyebrow {
    color: var(--text-muted, #94a3b8);
    font-size: 0.75rem;
    text-transform: uppercase;
  }

  .status-pill {
    border: 1px solid #f59e0b;
    border-radius: 999px;
    color: #fbbf24;
    padding: 4px 10px;
  }

  .status-pill.ready {
    border-color: #22c55e;
    color: #86efac;
  }
</style>
