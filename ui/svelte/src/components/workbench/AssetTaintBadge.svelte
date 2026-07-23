<script>
  let { taint } = $props();
  let severity = $derived(String(taint?.severity ?? 'info').toLowerCase());
</script>

<span class="taint taint-{severity}" title={taint?.reason ?? ''}>
  {severity}: {taint?.reason ?? 'unspecified'}
</span>

<style>
  .taint { display: inline-flex; padding: 2px 7px; border-radius: 999px; font-size: 0.7rem; border: 1px solid var(--border-default); color: var(--text-secondary); }
  .taint-warning { color: var(--warning); background: var(--warning-muted); }
  .taint-blocker { color: var(--danger); background: var(--danger-muted); }
</style>
