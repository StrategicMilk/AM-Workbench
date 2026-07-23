<script>
  import InactiveReasonBadge from './InactiveReasonBadge.svelte';
  import ProvenanceGate from '$lib/security/ProvenanceGate.svelte';
  import { provenanceDecision } from '$lib/security';
  import { asArray, errorMessage } from '$lib/utils/safe.js';

  const { choice, store, surface } = $props();

  let repinning = $state(false);
  let repinMessage = $state(null);
  const capabilitySummary = $derived(choice.capability_snapshot?.capabilities?.join(', ') ?? 'No capability snapshot');
  const provenanceRefs = $derived([
    ...(Array.isArray(choice?.evidence_refs) ? choice.evidence_refs : []),
    ...(Array.isArray(choice?.provenance_refs) ? choice.provenance_refs : []),
    choice?.policy_ref,
    choice?.pinned_version_id,
  ].filter(Boolean));
  const provenance = $derived(provenanceDecision({
    evidence_refs: provenanceRefs,
    status: choice?.trust_status ?? choice?.status ?? 'verified',
    reasons: choice?.inactive_reasons,
  }, `model-choice:${choice?.model_ref?.qualified_id ?? 'unknown'}`));
  const canRepin = $derived(Boolean(choice.is_active && choice.pinned_version_id && provenance.trusted));

  async function repin() {
    if (!canRepin) return;
    repinning = true;
    repinMessage = null;
    try {
      const decision = await store.repin(surface, choice.model_ref.qualified_id);
      repinMessage = decision.reasons?.join(', ') ?? (decision.repinned ? 'repinned' : 'unchanged');
    } catch (err) {
      repinMessage = errorMessage(err);
    } finally {
      repinning = false;
    }
  }
</script>

<article class:inactive={!choice.is_active} class="choice-row">
  <div class="choice-main">
    <h3>{choice.display_name}</h3>
    <p>{choice.model_ref.qualified_id}</p>
  </div>

  <div class="choice-meta">
    <span>{capabilitySummary}</span>
    {#if choice.pinned_version_id}
      <span>Pin {choice.pinned_version_id}</span>
    {/if}
  </div>

  <div class="choice-state">
    <ProvenanceGate
      refs={provenanceRefs}
      status={choice?.trust_status ?? choice?.status ?? 'verified'}
      reasons={choice?.inactive_reasons}
      context={`model-choice:${choice?.model_ref?.qualified_id ?? 'unknown'}`}
      compact
    />
    {#if choice.is_active}
      <span class="active-pill" role="status" aria-label={`${choice.display_name} is active`} aria-live="polite">Active</span>
    {:else}
      <div class="reason-list" aria-label="Inactive reasons">
        {#each asArray(choice.inactive_reasons) as reason (reason)}
          <InactiveReasonBadge {reason} />
        {/each}
      </div>
    {/if}
  </div>

  <button
    type="button"
    disabled={!canRepin || repinning}
    aria-label={`Repin ${choice.display_name}`}
    onclick={repin}
  >
    {repinning ? 'Repinning' : 'Repin'}
  </button>

  {#if repinMessage}
    <p class="repin-message" role="status" aria-live="polite">{repinMessage}</p>
  {/if}
</article>

<style>
  .choice-row {
    display: grid;
    grid-template-columns: minmax(170px, 1fr) minmax(190px, 1fr) minmax(180px, 0.9fr) 86px;
    gap: 10px;
    align-items: center;
    min-height: 82px;
    padding: 12px;
    border: 1px solid var(--border-default, #d6d9de);
    border-radius: 6px;
    background: var(--surface-primary, #fff);
  }

  .choice-row.inactive {
    border-color: var(--warning, #b45309);
  }

  .choice-main,
  .choice-meta,
  .choice-state {
    min-width: 0;
  }

  h3,
  p {
    margin: 0;
  }

  h3 {
    color: var(--text-primary, #111827);
    font-size: 0.9375rem;
  }

  p,
  .choice-meta {
    color: var(--text-muted, #4b5563);
    font-size: 0.75rem;
    overflow-wrap: anywhere;
  }

  .choice-meta {
    display: flex;
    flex-direction: column;
    gap: 3px;
  }

  .reason-list {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
  }

  .active-pill {
    display: inline-flex;
    align-items: center;
    min-height: 22px;
    padding: 2px 7px;
    border: 1px solid var(--success, #15803d);
    border-radius: 6px;
    background: rgba(21, 128, 61, 0.12);
    color: var(--text-primary, #111827);
    font-size: 0.6875rem;
    font-weight: 700;
  }

  button {
    width: 86px;
    min-height: 34px;
    border: 1px solid var(--border-default, #d6d9de);
    border-radius: 6px;
    background: var(--surface-secondary, #f8fafc);
    color: var(--text-primary, #111827);
    font: inherit;
    font-size: 0.8125rem;
    font-weight: 700;
  }

  button:not(:disabled) {
    cursor: pointer;
  }

  button:disabled {
    color: var(--text-muted, #6b7280);
    cursor: not-allowed;
  }

  .repin-message {
    grid-column: 1 / -1;
    color: var(--text-muted, #4b5563);
    font-size: 0.75rem;
  }

  @media (max-width: 860px) {
    .choice-row {
      grid-template-columns: 1fr;
    }

    button {
      width: 100%;
    }
  }
</style>
