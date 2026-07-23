<script>
  import { provenanceDecision } from './index.js';

  let {
    refs = [],
    status = '',
    trustTier = '',
    allowed = undefined,
    reasons = [],
    context = 'supply-chain',
    compact = false,
  } = $props();

  let decision = $derived(provenanceDecision({
    evidence_refs: refs,
    status,
    trust_tier: trustTier,
    allowed,
    reasons,
  }, context));
</script>

<section class:compact class="provenance-gate" data-provenance-state={decision.state} aria-label={`${context} provenance`}>
  <strong>{decision.state}</strong>
  {#if !compact}
    <span>{decision.refs.length} proof refs</span>
    {#if decision.reasons.length > 0}
      <small>{decision.reasons.join('; ')}</small>
    {/if}
  {/if}
</section>

<style>
  .provenance-gate {
    display: grid;
    gap: 3px;
    border: 1px solid var(--border-default, #334155);
    border-radius: 6px;
    padding: 7px 9px;
    background: var(--surface-elevated, #111827);
    color: var(--text-primary, #e5e7eb);
    font-size: 0.75rem;
  }

  .provenance-gate.compact {
    display: inline-flex;
    align-items: center;
    min-height: 24px;
    padding: 2px 7px;
  }

  [data-provenance-state='trusted'] {
    border-color: #31a66a;
  }

  [data-provenance-state='blocked'] {
    border-color: #dc2626;
  }

  [data-provenance-state='degraded'] {
    border-color: #d6a821;
  }

  strong {
    text-transform: uppercase;
  }

  span,
  small {
    color: var(--text-muted, #94a3b8);
    overflow-wrap: anywhere;
  }
</style>
