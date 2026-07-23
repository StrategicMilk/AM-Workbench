<script>
  // Props use $props(); do not replace with legacy export let in Svelte 5.
  let { passed, blockers = [] } = $props();
  const MAX_BLOCKERS = 20;
  let boundedBlockers = $derived(Array.isArray(blockers) ? blockers.slice(0, MAX_BLOCKERS) : []);

  let firstBlocker = $derived(boundedBlockers[0] ?? 'missing evidence');
  let extraCount = $derived(Math.max(boundedBlockers.length - 1, 0));
  let label = $derived(passed ? 'Gate passed' : `Gate blocked: ${firstBlocker}`);
</script>

<span class:passed class:blocked={!passed} class="promotion-gate-badge" role={passed ? 'status' : 'alert'} aria-live={passed ? 'polite' : 'assertive'} aria-label={label}>
  {#if passed}
    <span class="dot"></span>
    Gate: PASS
  {:else}
    <span class="dot"></span>
    Gate: BLOCKED - {firstBlocker}
    {#if extraCount > 0}
      <span class="more">+{extraCount} more</span>
    {/if}
  {/if}
</span>

<style>
  .promotion-gate-badge {
    align-items: center;
    border-radius: 6px;
    display: inline-flex;
    font-size: 0.78rem;
    font-weight: 700;
    gap: 0.4rem;
    line-height: 1.2;
    min-height: 1.75rem;
    padding: 0.35rem 0.55rem;
  }

  .promotion-gate-badge.passed {
    background: var(--success-muted);
    color: var(--success);
  }

  .promotion-gate-badge.blocked {
    background: var(--danger-muted);
    color: var(--danger);
  }

  .dot {
    border-radius: 50%;
    display: inline-block;
    height: 0.55rem;
    width: 0.55rem;
  }

  .passed .dot {
    background: var(--success);
  }

  .blocked .dot {
    background: var(--danger);
  }

  .more {
    background: var(--surface-elevated, rgba(255, 255, 255, 0.75));
    border-radius: 6px;
    padding: 0.15rem 0.35rem;
  }
</style>
