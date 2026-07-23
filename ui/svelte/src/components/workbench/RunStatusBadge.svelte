<script>
  let { status = 'pending' } = $props();
  let normalized = $derived(String(status ?? 'pending').toLowerCase());
</script>

<span class="run-status status-{normalized}" aria-label="Run status {normalized}">
  {normalized}
</span>

<style>
  .run-status {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 76px;
    padding: 3px 8px;
    border-radius: 999px;
    font-size: 0.72rem;
    font-weight: 700;
    line-height: 1.2;
    text-transform: uppercase;
    color: var(--text-primary);
    background: var(--surface-hover);
    border: 1px solid var(--border-default);
  }

  .status-succeeded { color: var(--success); background: var(--success-muted); }
  .status-running { color: var(--primary); background: var(--primary-muted); }
  .status-failed,
  .status-blocked { color: var(--danger); background: var(--danger-muted); }
  .status-cancelled { color: var(--text-muted); }
  .status-pending { color: var(--warning); background: var(--warning-muted); }
</style>
