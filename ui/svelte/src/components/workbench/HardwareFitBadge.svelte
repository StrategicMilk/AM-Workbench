<script>
  let { run } = $props();
  let metric = $derived((run?.metrics ?? []).find((item) => item.name === 'gpu_memory_fit') ?? null);
  let value = $derived(metric?.value ?? null);
  let tone = $derived(value === null ? null : value >= 0.9 ? 'green' : value >= 0.6 ? 'amber' : 'red');
  let fitLabel = $derived(
    value === null ? 'unknown fit' :
    value >= 0.9 ? 'high fit' :
    value >= 0.6 ? 'limited fit' : 'insufficient fit'
  );
</script>

{#if metric}
  <span class="hardware {tone}" role="status" aria-live="polite" aria-label={`GPU memory ${fitLabel}: ${metric.value}${metric.unit ? ` ${metric.unit}` : ''}`} title={`GPU memory ${fitLabel}`}>
    hardware {fitLabel} {metric.value}{metric.unit ? ` ${metric.unit}` : ''}
  </span>
{/if}

<style>
  .hardware { display: inline-flex; padding: 3px 8px; border-radius: 999px; font-size: 0.72rem; border: 1px solid var(--border-default); }
  .green { color: var(--success); background: var(--success-muted); }
  .amber { color: var(--warning); background: var(--warning-muted); }
  .red { color: var(--danger); background: var(--danger-muted); }
</style>
