<script>
  import { workbenchKernelRequest } from '$lib/api.js';

  let { projectId = new URLSearchParams(window.location.search).get('project_id') ?? 'default' } = $props();

  let sourceCards = $state([]);
  let toolCards = $state([]);
  let selectedSourceCardId = $state(null);
  let selectedToolCardId = $state(null);
  let claimKind = $state('summary');
  let caveatsAcknowledged = $state(false);
  let promotionDecision = $state(null);
  let loadError = $state(null);
  let freshOnly = $state(false);
  let loading = $state(true);

  let selectedSourceCard = $derived(sourceCards.find((card) => card.source_card_id === selectedSourceCardId) ?? null);
  let selectedToolCard = $derived(toolCards.find((card) => card.tool_card_id === selectedToolCardId) ?? null);
  let staleSourceCardCount = $derived(sourceCards.filter((card) => isStale(card)).length);

  function isStale(card) {
    if (card.observed_at_utc === null) return true;
    const observedAt = new Date(card.observed_at_utc).getTime();
    if (Number.isNaN(observedAt)) return true;
    return Date.now() - observedAt > card.freshness_max_age_seconds * 1000;
  }

  function freshnessClass(card) {
    if (card.observed_at_utc === null) return 'never_observed';
    return isStale(card) ? 'stale' : 'fresh';
  }

  async function getJson(path, params = {}) {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== null && value !== undefined && value !== '') query.set(key, value);
    }
    return workbenchKernelRequest(`${path}?${query.toString()}`);
  }

  async function fetchSourceCards() {
    sourceCards = await getJson('/api/workbench/source-cards', {
      project_id: projectId,
      fresh_only: freshOnly,
    });
    if (!selectedSourceCardId && sourceCards.length > 0) selectedSourceCardId = sourceCards[0].source_card_id;
  }

  async function fetchToolCards() {
    toolCards = await getJson('/api/workbench/tool-cards', { project_id: projectId });
    if (!selectedToolCardId && toolCards.length > 0) selectedToolCardId = toolCards[0].tool_card_id;
  }

  async function evaluateClaimPromotion() {
    if (!selectedToolCardId) return;
    const query = new URLSearchParams({
      project_id: projectId,
      claim_kind: claimKind,
      caveats_acknowledged: String(caveatsAcknowledged),
    });
    try {
      promotionDecision = await workbenchKernelRequest(
        `/api/workbench/tool-cards/${encodeURIComponent(selectedToolCardId)}/evaluate-claim-promotion?${query.toString()}`,
        { method: 'POST' },
      );
    } catch (err) {
      loadError = err instanceof Error ? err.message : String(err);
    }
  }

  $effect(() => {
    let cancelled = false;
    loading = true;
    loadError = null;
    promotionDecision = null;
    Promise.all([fetchSourceCards(), fetchToolCards()])
      .then(() => {
        if (!cancelled) loading = false;
      })
      .catch((err) => {
        if (cancelled) return;
        loadError = err.message ?? String(err);
        loading = false;
      });
    return () => { cancelled = true; };
  });
</script>

<main class="source-tool-cards">
  <header class="page-header">
    <div>
      <h2>Source And Tool Cards</h2>
      <p>{projectId}</p>
    </div>
    <div class="summary">
      <span>{sourceCards.length} sources</span>
      <span>{toolCards.length} tools</span>
      <span>{staleSourceCardCount} stale</span>
    </div>
  </header>

  {#if loading}
    <div class="state" role="status" aria-live="polite">Loading source and tool cards.</div>
  {:else if loadError}
    <div class="state error" role="alert">{loadError}</div>
  {:else}
    <div class="card-grid">
      <section aria-label="Source cards">
        <div class="section-header">
          <h3>Sources</h3>
          <label>
            <input type="checkbox" bind:checked={freshOnly} />
            Fresh only
          </label>
        </div>
        <div class="rows">
          {#each sourceCards as card (card.source_card_id)}
            <button
              class:selected={card.source_card_id === selectedSourceCardId}
              class="row"
              data-testid="source-card-row"
              type="button"
              aria-pressed={card.source_card_id === selectedSourceCardId}
              onclick={() => { selectedSourceCardId = card.source_card_id; }}
            >
              <span class="row-title">{card.name}</span>
              <span class="meta">{card.kind}</span>
              <span class={`freshness ${freshnessClass(card)}`} data-testid="freshness-badge">
                {freshnessClass(card)}
              </span>
              <span class="muted">{card.can_answer.length} answers / {card.cannot_answer.length} limits</span>
              <span class="muted">{card.caveats.length} caveats / credentials: {card.credential_exposure}</span>
            </button>
          {/each}
        </div>
      </section>

      <section aria-label="Tool cards">
        <div class="section-header">
          <h3>Tools</h3>
        </div>
        <div class="rows">
          {#each toolCards as card (card.tool_card_id)}
            <button
              class:selected={card.tool_card_id === selectedToolCardId}
              class="row"
              data-testid="tool-card-row"
              type="button"
              aria-pressed={card.tool_card_id === selectedToolCardId}
              onclick={() => { selectedToolCardId = card.tool_card_id; }}
            >
              <span class="row-title">{card.name}</span>
              <span class="meta">{card.kind}</span>
              <span class="muted">sources: {card.source_card_ids.join(', ') || 'none'}</span>
              <span class="chips">
                {#each card.permitted_claim_kinds as permitted}
                  <span>{permitted}</span>
                {:else}
                  <span>observations only</span>
                {/each}
              </span>
              <span class="muted">
                freshness {card.requires_freshness_pass ? 'required' : 'optional'} /
                provenance {card.requires_provenance_present ? 'required' : 'optional'} /
                caveats {card.requires_caveats_acknowledged ? 'required' : 'optional'}
              </span>
            </button>
          {/each}
        </div>
      </section>
    </div>

    <section class="promotion-panel" aria-label="Promote claim">
      <div>
        <h3>Promote Claim</h3>
        <p>{selectedToolCard ? selectedToolCard.name : 'No tool selected'}{selectedSourceCard ? ` / ${selectedSourceCard.name}` : ''}</p>
      </div>
      <label>
        Claim kind
        <input bind:value={claimKind} />
      </label>
      <label class="check">
        <input type="checkbox" bind:checked={caveatsAcknowledged} />
        Caveats acknowledged
      </label>
      <button data-testid="evaluate-claim-button" type="button" onclick={evaluateClaimPromotion} disabled={!selectedToolCardId}>
        Evaluate
      </button>
      {#if promotionDecision}
        {#if promotionDecision.passed}
          <div class="decision pass">PROMOTED to {promotionDecision.permitted_claim_kind}</div>
        {:else}
          <div class="decision reject" data-testid="rejection-reasons">
            {#each promotionDecision.rejection_reasons as reason}
              <div>{reason}</div>
            {/each}
          </div>
        {/if}
      {:else}
        <div class="decision muted" data-testid="rejection-reasons">No decision yet.</div>
      {/if}
    </section>
  {/if}
</main>

<style>
  .source-tool-cards { padding: 18px; max-width: 1440px; display: flex; flex-direction: column; gap: 14px; }
  .page-header, .section-header, .promotion-panel { display: flex; justify-content: space-between; gap: 12px; align-items: center; flex-wrap: wrap; }
  h2, h3, p { margin: 0; }
  h2 { font-size: 1.25rem; color: var(--text-primary); }
  h3 { font-size: 1rem; color: var(--text-primary); }
  p, .muted { color: var(--text-muted); font-size: 0.82rem; }
  .summary, .chips { display: flex; gap: 6px; flex-wrap: wrap; }
  .summary span, .chips span, .meta { border: 1px solid var(--border-default); border-radius: 6px; padding: 3px 7px; color: var(--text-muted); font-size: 0.78rem; }
  .card-grid { display: grid; grid-template-columns: minmax(320px, 1fr) minmax(320px, 1fr); gap: 14px; align-items: start; }
  section { min-width: 0; }
  .rows { display: flex; flex-direction: column; gap: 8px; margin-top: 10px; }
  .row { width: 100%; min-height: 118px; text-align: left; display: flex; flex-direction: column; gap: 6px; padding: 12px; border: 1px solid var(--border-default); border-radius: 8px; background: var(--surface-elevated); color: var(--text-primary); }
  .row.selected { border-color: var(--accent); }
  .row-title { font-weight: 650; overflow-wrap: anywhere; }
  .freshness { width: fit-content; border-radius: 6px; padding: 3px 7px; font-size: 0.78rem; }
  .fresh { background: rgba(25, 135, 84, 0.14); color: #2f9e44; }
  .stale { background: rgba(245, 158, 11, 0.16); color: #b7791f; }
  .never_observed { background: rgba(220, 53, 69, 0.14); color: #d9480f; }
  .state, .promotion-panel { padding: 16px; border: 1px solid var(--border-default); border-radius: 8px; background: var(--surface-elevated); }
  .error, .reject { color: var(--danger); }
  .pass { color: #2f9e44; }
  label { display: flex; flex-direction: column; gap: 4px; color: var(--text-muted); font-size: 0.82rem; }
  .check { flex-direction: row; align-items: center; }
  input { min-height: 44px; border: 1px solid var(--border-default); border-radius: 6px; background: var(--surface-default); color: var(--text-primary); padding: 4px 8px; }
  button { cursor: pointer; }
  .promotion-panel button { min-height: 44px; border: 1px solid var(--accent); border-radius: 6px; padding: 4px 12px; color: var(--text-primary); background: var(--surface-default); }
  .decision { min-width: 220px; font-size: 0.86rem; }
  @media (max-width: 900px) { .card-grid { grid-template-columns: 1fr; } }
</style>
