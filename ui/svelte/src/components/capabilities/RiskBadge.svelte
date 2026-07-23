<script>
  let { risk = 'moderate' } = $props();
  const palette = {
    low: 'Low risk',
    moderate: 'Moderate risk',
    high: 'High risk',
    critical: 'Critical risk',
  };
  let label = $derived(palette[risk] ?? palette.moderate);
</script>

<span class={`risk-badge risk-${risk}`} data-testid={`risk-badge-${risk}`}>{label}</span>

<style>
  .risk-badge { display: inline-flex; border-radius: 999px; padding: 0.125rem 0.5rem; font-size: 0.75rem; font-weight: 700; }
  .risk-low { color: #14532d; background: #dcfce7; }
  .risk-moderate { color: #713f12; background: #fef9c3; }
  .risk-high { color: #7c2d12; background: #ffedd5; }
  .risk-critical { color: #7f1d1d; background: #fee2e2; }
</style>
