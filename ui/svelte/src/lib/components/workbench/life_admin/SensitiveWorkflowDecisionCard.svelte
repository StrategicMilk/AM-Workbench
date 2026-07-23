<script>
  import { RigorRequired } from '$lib/contracts';

  let { decision = null } = $props();

  const decisionLabels = {
    allowed: 'Allowed',
    denied_missing_context: 'Needs more context',
    denied_authority_required: 'Authority required',
    denied_evidence_required: 'Evidence required',
    denied_freshness_failed: 'Freshness failed',
    denied_unknown_jurisdiction: 'Jurisdiction required',
    denied_promotion_blocked: 'Promotion blocked',
    degraded_unreadable_policy: 'Policy unreadable',
  };

  let isHighRigor = $derived(
    decision?.rigor_required === RigorRequired.CHECK_IT_CAREFULLY || decision?.rigor_required === RigorRequired.MAKE_IT_REUSABLE,
  );
  let denialReasons = $derived(Array.isArray(decision?.denial_reasons) ? decision.denial_reasons : []);
  let missingContext = $derived(Array.isArray(decision?.missing_context) ? decision.missing_context : []);
</script>

<section class="decision-card" aria-label="Sensitive workflow decision" aria-live="polite">
  {#if decision}
    {#if decision.degraded}
      <div class="banner degraded" role="alert">Policy state unreadable - rerun later.</div>
    {/if}

    <header>
      <div>
        <h3>{decisionLabels[decision.decision_kind] ?? decision.decision_kind}</h3>
        <p>{decision.decision_kind}</p>
      </div>
      <span class:allowed={decision.allowed} class:denied={!decision.allowed} role="status" aria-label={`Decision ${decision.allowed ? 'allowed' : 'denied'}`}>
        {decision.allowed ? 'Allowed' : 'Denied'}
      </span>
    </header>

    <dl>
      <div>
        <dt>Decision kind</dt>
        <dd>{decision.decision_kind}</dd>
      </div>
      <div>
        <dt>Rigor required</dt>
        <dd>{decision.rigor_required}{isHighRigor ? ' - High-rigor mode' : ''}</dd>
      </div>
      <div>
        <dt>Policy explanation</dt>
        <dd>{decision.policy_explanation_ref || 'unavailable'}</dd>
      </div>
    </dl>

    <section aria-label="Denial reasons">
      <h4>Denial reasons</h4>
      {#if denialReasons.length}
        <ul>
          {#each denialReasons as reason}
            <li>{reason}</li>
          {/each}
        </ul>
      {:else}
        <p>No denial reasons returned.</p>
      {/if}
    </section>

    <section aria-label="Missing context">
      <h4>Missing context</h4>
      {#if missingContext.length}
        <ul>
          {#each missingContext as item}
            <li>{item}</li>
          {/each}
        </ul>
      {:else}
        <p>No missing context returned.</p>
      {/if}
    </section>
  {:else}
    <div class="empty" role="status">No decision yet.</div>
  {/if}
</section>

<style>
  .decision-card {
    display: grid;
    gap: 12px;
    padding: 14px;
    border: 1px solid var(--border-color, #d1d5db);
    border-radius: 8px;
    background: var(--surface-primary, #fff);
    color: var(--text-primary, #111827);
  }

  header,
  dl {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 10px;
    align-items: start;
  }

  dl {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  h3,
  h4,
  p,
  dl,
  dd,
  ul {
    margin: 0;
  }

  h3 {
    font-size: 1rem;
  }

  h4,
  dt {
    font-size: 0.78rem;
    font-weight: 700;
  }

  p,
  dd {
    color: var(--text-secondary, #4b5563);
  }

  span,
  .banner {
    border-radius: 6px;
    padding: 5px 8px;
    font-size: 0.78rem;
    font-weight: 700;
  }

  .allowed {
    background: rgba(25, 135, 84, 0.14);
    color: #2f9e44;
  }

  .denied,
  .degraded {
    background: rgba(220, 53, 69, 0.14);
    color: #d9480f;
  }

  .empty {
    color: var(--text-secondary, #4b5563);
  }

  @media (max-width: 760px) {
    header,
    dl {
      grid-template-columns: 1fr;
    }
  }
</style>
