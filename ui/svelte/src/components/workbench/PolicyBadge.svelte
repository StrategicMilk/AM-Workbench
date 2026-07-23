<script>
  let { run } = $props();
  let metric = $derived((run?.metrics ?? []).find((item) => item.name === 'policy_decision') ?? null);
  let decision = $derived(metric ? normalizePolicyDecision(metric.value) : null);

  function normalizePolicyDecision(value) {
    if (typeof value === 'number' && Number.isFinite(value)) {
      return value >= 0.5 ? 'allowed' : 'blocked';
    }

    const normalized = String(value ?? '').trim().toLowerCase();
    if (['allow', 'allowed', 'pass', 'passed', 'success', 'true'].includes(normalized)) {
      return 'allowed';
    }
    if (['deny', 'denied', 'block', 'blocked', 'fail', 'failed', 'false'].includes(normalized)) {
      return 'blocked';
    }

    const numeric = Number(normalized);
    if (Number.isFinite(numeric)) {
      return numeric >= 0.5 ? 'allowed' : 'blocked';
    }

    return 'unknown';
  }
</script>

{#if metric}
  <span class="policy policy-{decision}" role="status" aria-label={`Policy decision ${decision}`} title={`Policy decision: ${decision}`}>
    policy {decision}
  </span>
{/if}

<style>
  .policy { display: inline-flex; padding: 3px 8px; border-radius: 999px; font-size: 0.72rem; border: 1px solid var(--border-default); }
  .policy-allowed { color: var(--success); background: var(--success-muted); }
  .policy-blocked { color: var(--danger); background: var(--danger-muted); }
  .policy-unknown { color: var(--text-muted); background: var(--surface-hover); }
</style>
