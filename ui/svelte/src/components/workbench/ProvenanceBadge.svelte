<script>
  let { record, onClick = () => {} } = $props();
  let recordId = $derived(record?.run_id ?? record?.asset_id ?? record?.trace_id ?? record?.eval_id ?? record?.proposal_id ?? record?.lease_id ?? 'record');
</script>

{#if record}
  <button
    type="button"
    class="provenance"
    data-testid="provenance-badge-{recordId}"
    aria-label="Open provenance source {recordId}"
    onclick={() => onClick(record)}
  >
    source {recordId}
  </button>
{/if}

<style>
  .provenance { border: 1px solid var(--border-default); border-radius: 999px; padding: 3px 8px; background: var(--surface-elevated); color: var(--primary); font: inherit; font-size: 0.72rem; cursor: pointer; }
</style>
