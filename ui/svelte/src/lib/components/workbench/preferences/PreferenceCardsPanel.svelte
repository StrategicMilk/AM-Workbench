<script>
  import Icon from '$lib/a11y/Icon.svelte';
  import * as api from '$lib/api.js';
  import { CardStatus } from '$lib/contracts';
  import { requireEvidence } from '$lib/evidence/evidenceGuard.js';

  let { projectId = 'default' } = $props();

  const STATUS_FILTERS = [CardStatus.ALL, CardStatus.PROPOSED, CardStatus.ACTIVE, CardStatus.REVOKED, CardStatus.EXPIRED];

  let cards = $state([]);
  let activeFilter = $state('all');
  let loading = $state(true);
  let error = $state('');

  let visibleCards = $derived(
    activeFilter === 'all' ? cards : cards.filter((card) => card.status === activeFilter)
  );

  function validateCardEvidence(nextCards) {
    const refs = nextCards.flatMap((card) => card.evidence ?? card.evidence_refs ?? []);
    if (refs.length > 0) {
      requireEvidence(refs, 'preference_cards.evidence_refs');
    }
  }

  async function loadCards() {
    loading = true;
    error = '';
    try {
      const snapshot = await api.getWorkbenchPreferenceCardsSnapshot(projectId);
      const nextCards = Array.isArray(snapshot?.cards) ? snapshot.cards : [];
      validateCardEvidence(nextCards);
      cards = nextCards;
    } catch (err) {
      cards = [];
      error = err instanceof Error ? err.message : 'Preference card service unavailable';
    } finally {
      loading = false;
    }
  }

  function statusClass(status) {
    return `status-pill status-${status || 'unknown'}`;
  }

  function effectLabel(effect) {
    return String(effect || '').replaceAll('_', ' ');
  }

  $effect(() => {
    projectId;
    loadCards();
  });
</script>

<section class="preference-cards-panel" aria-labelledby="preference-cards-title">
  <header class="panel-header">
    <div>
      <h1 id="preference-cards-title">Preference Cards</h1>
      <p>Consent, scope, and decay for learned Workbench preferences.</p>
    </div>
    <button class="refresh-button" type="button" onclick={loadCards} disabled={loading} title="Refresh preference cards">
      <Icon name="rotate" class={loading ? 'fa-spin' : ''} />
      <span>Refresh</span>
    </button>
  </header>

  <div class="filter-row" role="tablist" aria-label="Preference card status filters">
    {#each STATUS_FILTERS as status}
      <button
        class:active={activeFilter === status}
        role="tab"
        aria-selected={activeFilter === status}
        tabindex={activeFilter === status ? 0 : -1}
        onclick={() => { activeFilter = status; }}
      >
        {status}
      </button>
    {/each}
  </div>

  {#if error}
    <div class="notice notice-blocked" role="alert">
      <Icon name="lock" />
      <span>Preference cards are unavailable. Downstream effects remain blocked.</span>
    </div>
  {:else if loading}
    <div class="notice" role="status">Loading preference cards...</div>
  {:else if visibleCards.length === 0}
    <div class="notice" role="status">No preference cards match this view.</div>
  {:else}
    <div class="card-grid">
      {#each visibleCards as card}
        <article class="preference-card">
          <header class="card-header">
            <div>
              <h2>{card.label}</h2>
              <p>{card.statement}</p>
            </div>
            <span class={statusClass(card.status)}>{card.status}</span>
          </header>

          <dl class="meta-grid">
            <div>
              <dt>Scope</dt>
              <dd>{card.scope?.scope_type || 'unknown'}</dd>
            </div>
            <div>
              <dt>Confidence</dt>
              <dd>{Math.round((card.confidence || 0) * 100)}%</dd>
            </div>
            <div>
              <dt>Consent</dt>
              <dd>{card.consent?.granted ? 'granted' : 'not granted'}</dd>
            </div>
            <div>
              <dt>Decay</dt>
              <dd>{card.decay_policy?.max_age_days || 0}d max</dd>
            </div>
          </dl>

          <div class="effect-row" aria-label="Downstream effects">
            {#each card.downstream_effects || [] as effect}
              <span>{effectLabel(effect)}</span>
            {/each}
          </div>

          <footer class="card-footer">
            <span>{card.evidence?.length || 0} evidence items</span>
            {#if card.revoke_path}
              <a href={card.revoke_path} aria-label={`Revoke preference card ${card.label}`}>Revoke</a>
            {:else}
              <span role="status">Revoke unavailable</span>
            {/if}
          </footer>
        </article>
      {/each}
    </div>
  {/if}
</section>

<style>
  .preference-cards-panel {
    display: flex;
    flex-direction: column;
    gap: 18px;
    padding: 24px;
    color: var(--text-primary);
  }

  .panel-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 16px;
  }

  .panel-header h1 {
    margin: 0 0 6px;
    font-size: 28px;
    font-weight: 700;
  }

  .panel-header p {
    margin: 0;
    color: var(--text-secondary);
  }

  .refresh-button,
  .filter-row button {
    border: 1px solid var(--border-default);
    background: var(--surface-elevated);
    color: var(--text-primary);
    border-radius: 8px;
    cursor: pointer;
  }

  .refresh-button {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 9px 12px;
  }

  .filter-row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  .filter-row button {
    padding: 8px 11px;
    text-transform: capitalize;
  }

  .filter-row button.active {
    border-color: var(--accent-primary);
    background: var(--accent-muted, rgba(88, 166, 255, 0.15));
  }

  .notice {
    border: 1px solid var(--border-default);
    background: var(--surface-elevated);
    border-radius: 8px;
    padding: 16px;
    color: var(--text-secondary);
  }

  .notice-blocked {
    display: flex;
    gap: 10px;
    color: var(--warning, #f0b429);
  }

  .card-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 14px;
  }

  .preference-card {
    border: 1px solid var(--border-default);
    background: var(--surface-elevated);
    border-radius: 8px;
    padding: 16px;
  }

  .card-header {
    display: flex;
    justify-content: space-between;
    gap: 12px;
  }

  .card-header h2 {
    margin: 0 0 6px;
    font-size: 17px;
  }

  .card-header p {
    margin: 0;
    color: var(--text-secondary);
    line-height: 1.4;
  }

  .status-pill,
  .effect-row span {
    border-radius: 999px;
    border: 1px solid var(--border-default);
    padding: 4px 8px;
    font-size: 12px;
    white-space: nowrap;
  }

  .status-active {
    color: var(--success, #3fb950);
  }

  .status-proposed {
    color: var(--accent-primary);
  }

  .status-revoked,
  .status-expired {
    color: var(--warning, #f0b429);
  }

  .meta-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
    margin: 16px 0;
  }

  .meta-grid dt {
    color: var(--text-muted);
    font-size: 12px;
  }

  .meta-grid dd {
    margin: 3px 0 0;
    overflow-wrap: anywhere;
  }

  .effect-row {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .card-footer {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    margin-top: 16px;
    color: var(--text-secondary);
    font-size: 13px;
  }

  .card-footer a {
    color: var(--accent-primary);
  }

  @media (max-width: 640px) {
    .preference-cards-panel {
      padding: 16px;
    }

    .panel-header,
    .card-header,
    .card-footer {
      flex-direction: column;
      align-items: stretch;
    }
  }
</style>
