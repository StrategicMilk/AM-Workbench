<script>
  let { selectedRun = null, runCount = 0, assetCount = 0 } = $props();
</script>

<aside class="status-pane" aria-label="Workbench console status">
  <span>{runCount} runs</span>
  <span>{assetCount} assets</span>
  <span>selected: {selectedRun?.run_id ?? 'none'}</span>
  {#if selectedRun}
    <strong>{selectedRun.status}</strong>
    <strong>{selectedRun.kind}</strong>
  {/if}
</aside>

<style>
  .status-pane { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; color: var(--text-muted); font-size: 0.78rem; }
  span, strong { border: 1px solid var(--border-default); border-radius: 999px; padding: 4px 8px; background: var(--surface-elevated); }
  strong { color: var(--text-primary); font-weight: 700; }
</style>
