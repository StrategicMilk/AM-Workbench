<script>
  import { asArray, finiteNumber, nonEmptyString } from '$lib/utils/safe.js';

  const { plan = undefined, selectedCandidate = undefined, onSelect = undefined } = $props();

  let activeModelId = $state(selectedCandidate ?? plan?.recommended_model_id ?? '');

  const candidates = $derived(asArray(plan?.candidates));
  const activeCandidate = $derived(
    candidates.find((candidate) => candidate?.model_id === activeModelId) ?? candidates[0] ?? undefined,
  );
  const adjustments = $derived(asArray(plan?.pressure_adjustments));

  function selectCandidate(candidate) {
    if (!candidate || typeof candidate !== 'object') return;
    activeModelId = candidate.model_id;
    if (typeof onSelect === 'function') {
      onSelect(candidate);
    }
  }

  function money(value) {
    return `$${Math.max(0, finiteNumber(value, 0)).toFixed(4)}`;
  }

  function quantity(value, fallback = 0) {
    return Math.max(0, finiteNumber(value, fallback));
  }
</script>

<section class="cost-planner-panel" aria-label="Cost and resource planner">
  <header class="plan-header">
    <div>
      <h2>{plan?.plan_id ?? 'Cost plan'}</h2>
      <p>{plan?.workload_kind ?? 'unplanned'} · {plan?.budget_status ?? 'unknown'}</p>
    </div>
    <strong class:approval={plan?.approval_required}>{plan?.approval_required ? 'Approval required' : 'Within budget'}</strong>
  </header>

  {#if candidates.length === 0}
    <p class="empty-state" role="status">No forecast candidates.</p>
  {:else}
    <div class="candidate-tabs" role="tablist" aria-label="Forecast candidates">
      {#each candidates as candidate ((candidate?.backend ?? 'unknown') + (candidate?.model_id ?? 'unknown'))}
        <button
          type="button"
          class:active={activeCandidate?.model_id === candidate?.model_id}
          onclick={() => selectCandidate(candidate)}
        >
          {nonEmptyString(candidate?.backend, 'unknown')}:{nonEmptyString(candidate?.model_id, 'unknown')}
        </button>
      {/each}
    </div>

    <dl class="forecast-grid">
      <div>
        <dt>Cost</dt>
        <dd>{money(activeCandidate?.total_cost_usd)}</dd>
      </div>
      <div>
        <dt>Latency</dt>
        <dd>{Math.round(quantity(activeCandidate?.total_latency_ms))} ms</dd>
      </div>
      <div>
        <dt>GPU</dt>
        <dd>{quantity(activeCandidate?.gpu_hours)} h</dd>
      </div>
      <div>
        <dt>CPU</dt>
        <dd>{quantity(activeCandidate?.cpu_core_hours)} h</dd>
      </div>
      <div>
        <dt>RAM</dt>
        <dd>{quantity(activeCandidate?.ram_gb)} GB</dd>
      </div>
      <div>
        <dt>Queue</dt>
        <dd>{quantity(activeCandidate?.queue_minutes)} min</dd>
      </div>
    </dl>

    <div class="pressure-table" aria-label="Cost pressure changes">
      {#each adjustments as adjustment (adjustment.action + adjustment.reason)}
        <article>
          <span>{nonEmptyString(adjustment.action, 'unknown action')}</span>
          <p>{nonEmptyString(adjustment.before, 'unknown')} → {nonEmptyString(adjustment.after, 'unknown')}</p>
        </article>
      {/each}
    </div>
  {/if}
</section>

<style>
  .cost-planner-panel {
    display: grid;
    gap: 12px;
    width: 100%;
  }

  .plan-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--border-default, #d6d9de);
  }

  .plan-header h2 {
    margin: 0 0 2px;
    color: var(--text-primary, #111827);
    font-size: 1rem;
    font-weight: 700;
  }

  .plan-header p {
    margin: 0;
    color: var(--text-muted, #4b5563);
    font-size: 0.8125rem;
  }

  .plan-header strong {
    color: var(--success, #047857);
    font-size: 0.8125rem;
    white-space: nowrap;
  }

  .plan-header strong.approval {
    color: var(--danger, #dc2626);
  }

  .candidate-tabs {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .candidate-tabs button {
    min-height: 32px;
    border: 1px solid var(--border-default, #d6d9de);
    border-radius: 6px;
    background: var(--surface-secondary, #f8fafc);
    color: var(--text-primary, #111827);
    font: inherit;
    font-size: 0.75rem;
    font-weight: 700;
    cursor: pointer;
  }

  .candidate-tabs button.active {
    border-color: var(--primary, #2563eb);
    background: var(--primary-soft, rgba(37, 99, 235, 0.12));
  }

  .forecast-grid {
    display: grid;
    grid-template-columns: repeat(6, minmax(88px, 1fr));
    gap: 8px;
    margin: 0;
  }

  .forecast-grid div {
    min-height: 56px;
    padding: 8px;
    border: 1px solid var(--border-default, #d6d9de);
    border-radius: 6px;
    background: var(--surface-primary, #fff);
  }

  .forecast-grid dt {
    color: var(--text-muted, #6b7280);
    font-size: 0.6875rem;
  }

  .forecast-grid dd {
    margin: 4px 0 0;
    color: var(--text-primary, #111827);
    font-size: 0.875rem;
    font-weight: 700;
    overflow-wrap: anywhere;
  }

  .pressure-table {
    display: grid;
    gap: 6px;
  }

  .pressure-table article {
    display: grid;
    grid-template-columns: minmax(128px, auto) minmax(0, 1fr);
    gap: 8px;
    align-items: center;
    min-height: 36px;
    padding: 8px;
    border: 1px solid var(--border-default, #d6d9de);
    border-radius: 6px;
    background: var(--surface-secondary, #f8fafc);
  }

  .pressure-table span {
    color: var(--text-primary, #111827);
    font-size: 0.75rem;
    font-weight: 700;
  }

  .pressure-table p,
  .empty-state {
    margin: 0;
    color: var(--text-muted, #4b5563);
    font-size: 0.8125rem;
    overflow-wrap: anywhere;
  }

  .candidate-tabs button:focus-visible {
    border-color: var(--primary, #2563eb);
    outline: 2px solid var(--primary-soft, rgba(37, 99, 235, 0.22));
    outline-offset: 2px;
  }

  @media (max-width: 760px) {
    .plan-header {
      align-items: flex-start;
      flex-direction: column;
    }

    .forecast-grid,
    .pressure-table article {
      grid-template-columns: 1fr 1fr;
    }
  }
</style>
