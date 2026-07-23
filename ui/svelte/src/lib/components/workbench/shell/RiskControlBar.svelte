<script>
  import { nonEmptyString } from '$lib/utils/safe.js';

  let { risk = null } = $props();

  let blocked = $derived(!risk || risk?.can_execute !== true);
  let riskLabel = $derived(nonEmptyString(risk?.risk_level, 'unknown'));
  let why = $derived(blocked && !risk ? 'Blocked until risk context is loaded.' : nonEmptyString(risk?.why, 'Context is not loaded.'));
</script>

<section class="risk-bar" class:blocked={blocked} aria-label="Risk and provenance controls" role={blocked ? 'alert' : 'status'} aria-live="polite" data-testid="workbench-risk-control">
  <div>
    <span class="label">Risk</span>
    <strong>{riskLabel}</strong>
  </div>
  <div>
    <span class="label">Cost</span>
    <strong>{nonEmptyString(risk?.cost_context, 'missing')}</strong>
  </div>
  <div>
    <span class="label">Provenance</span>
    <strong>{nonEmptyString(risk?.provenance_context, 'missing')}</strong>
  </div>
  <div>
    <span class="label">Policy</span>
    <strong>{nonEmptyString(risk?.policy_context, 'missing')}</strong>
  </div>
  <p>{why}</p>
</section>

<style>
  .risk-bar {
    display: grid;
    grid-template-columns: repeat(4, minmax(120px, 1fr)) minmax(220px, 1.4fr);
    gap: 8px;
    align-items: stretch;
    border: 1px solid var(--success);
    border-radius: 8px;
    background: var(--success-muted);
    padding: 10px;
  }

  .risk-bar.blocked {
    border-color: var(--warning);
    background: var(--warning-muted);
  }

  div {
    display: grid;
    gap: 2px;
    min-width: 0;
  }

  .label {
    color: var(--text-muted);
    font-size: 0.72rem;
    text-transform: uppercase;
  }

  strong,
  p {
    margin: 0;
    overflow-wrap: anywhere;
  }

  strong {
    color: var(--text-primary);
    font-size: 0.86rem;
  }

  p {
    color: var(--text-muted);
    font-size: 0.82rem;
  }

  @media (max-width: 980px) {
    .risk-bar {
      grid-template-columns: 1fr 1fr;
    }
  }
</style>
