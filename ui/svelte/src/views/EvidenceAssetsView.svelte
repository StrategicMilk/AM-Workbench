<script>
  import { showToast } from '$lib/stores/toast.svelte.js';
  import { browserLocationParam } from '$lib/utils/browser.js';
  import { workbenchKernelRequest } from '$lib/api.js';

  let { projectId = browserLocationParam('project_id', 'default') } = $props();

  let cards = $state([]);
  let kindCatalog = $state([]);
  let selectedCardId = $state(null);
  let filterKind = $state(null);
  let filterProofStatus = $state(null);
  let filterTaintsPresent = $state(null);
  let loading = $state(true);
  let error = $state(null);
  let failureHistoryDetail = $state(null);

  const proofStatusOrder = ['failed', 'unknown', 'unverified', 'partially_verified', 'verified'];

  let selectedCard = $derived(cards.find((card) => card.asset_card_id === selectedCardId) ?? null);
  let cardsByProofStatus = $derived(proofStatusOrder.reduce((acc, status) => {
    acc[status] = cards.filter((card) => card.proof_status === status);
    return acc;
  }, {}));
  let countsByKind = $derived(cards.reduce((acc, card) => {
    acc[card.kind] = (acc[card.kind] ?? 0) + 1;
    return acc;
  }, {}));

  function kindLabel(kind) {
    return kindCatalog.find((entry) => entry.id === kind)?.display_label ?? kind;
  }

  function proofStatusLabel(status) {
    return status.replaceAll('_', ' ');
  }

  function workbenchUrl(path, params = {}) {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== null && value !== undefined && value !== '') query.set(key, value);
    }
    return query.toString() ? `${path}?${query.toString()}` : path;
  }

  $effect(() => {
    let cancelled = false;
    loading = true;
    error = null;
    Promise.all([
      workbenchKernelRequest(workbenchUrl('/api/workbench/evidence-assets', {
        project_id: projectId,
        kind: filterKind,
        proof_status: filterProofStatus,
        taints_present: filterTaintsPresent === null ? null : filterTaintsPresent ? 'true' : 'false',
      })),
      workbenchKernelRequest('/api/workbench/evidence-assets/kinds'),
    ]).then(([cardRows, kindRows]) => {
      if (cancelled) return;
      cards = cardRows;
      kindCatalog = kindRows;
      if (!selectedCardId && cardRows.length > 0) selectedCardId = cardRows[0].asset_card_id;
      if (selectedCardId && !cardRows.some((card) => card.asset_card_id === selectedCardId)) selectedCardId = cardRows[0]?.asset_card_id ?? null;
      loading = false;
    }).catch((err) => {
      if (cancelled) return;
      error = err.message ?? String(err);
      showToast(`Evidence asset load failed: ${error}`, 'error');
      loading = false;
    });
    return () => { cancelled = true; };
  });

  $effect(() => {
    if (!selectedCardId) {
      failureHistoryDetail = null;
      return;
    }
    let cancelled = false;
    workbenchKernelRequest(workbenchUrl(`/api/workbench/evidence-assets/${encodeURIComponent(selectedCardId)}/failure-history`, { project_id: projectId }))
      .then((rows) => { if (!cancelled) failureHistoryDetail = rows; })
      .catch((err) => { if (!cancelled) showToast(`Failure history fetch failed: ${err.message ?? err}`, 'error'); });
    return () => { cancelled = true; };
  });
</script>

<div class="evidence-assets">
  <header class="asset-header">
    <div>
      <h2>Evidence Asset Library</h2>
      <p>{projectId}</p>
    </div>
    <div class="summary">
      <span>{cards.length} cards</span>
      <span>{kindCatalog.length} kinds</span>
    </div>
  </header>

  <section class="filters" aria-label="Evidence asset filters">
    <label>
      <span>Kind</span>
      <select bind:value={filterKind}>
        <option value={null}>Any kind</option>
        {#each kindCatalog as kind (kind.id)}
          <option value={kind.id}>{kind.display_label}</option>
        {/each}
      </select>
    </label>
    <label>
      <span>Proof status</span>
      <select bind:value={filterProofStatus}>
        <option value={null}>Any status</option>
        {#each proofStatusOrder as status}
          <option value={status}>{proofStatusLabel(status)}</option>
        {/each}
      </select>
    </label>
    <label>
      <span>Taints</span>
      <select bind:value={filterTaintsPresent}>
        <option value={null}>Any</option>
        <option value={true}>With taints</option>
        <option value={false}>Without taints</option>
      </select>
    </label>
  </section>

  {#if loading}
    <div class="state">Loading evidence-backed asset cards.</div>
  {:else if error}
    <div class="state error">{error}</div>
  {:else}
    <div class="asset-layout">
      <aside class="maturity-lanes" aria-label="Proof status maturity lanes">
        {#each proofStatusOrder as status}
          <button
            type="button"
            class:active={filterProofStatus === status}
            aria-pressed={filterProofStatus === status}
            data-testid={`proof-status-lane-${status}`}
            onclick={() => { filterProofStatus = filterProofStatus === status ? null : status; }}
          >
            <span>{proofStatusLabel(status)}</span>
            <strong>{cardsByProofStatus[status]?.length ?? 0}</strong>
          </button>
        {/each}
      </aside>

      <main class="card-list" aria-label="Evidence asset cards">
        {#each cards as card (card.asset_card_id)}
          <article
            class:selected={selectedCardId === card.asset_card_id}
            aria-current={selectedCardId === card.asset_card_id ? 'true' : undefined}
            data-testid={`asset-card-${card.asset_card_id}`}
            class="asset-card"
          >
            <header>
              <div>
                <span class="kind">{kindLabel(card.kind)}</span>
                <h3>{card.name}</h3>
              </div>
              <div class="card-actions">
                <span class={`proof ${card.proof_status}`} data-testid={`proof-status-${card.asset_card_id}`}>
                  {proofStatusLabel(card.proof_status)}
                </span>
                <button
                  type="button"
                  class="select-card"
                  aria-pressed={selectedCardId === card.asset_card_id}
                  onclick={() => { selectedCardId = card.asset_card_id; }}
                >
                  Select
                </button>
              </div>
            </header>

            <dl class="facts">
              <div>
                <dt>revision</dt>
                <dd>{card.revision}</dd>
              </div>
              <div>
                <dt>created</dt>
                <dd>{card.created_at_utc}</dd>
              </div>
              <div>
                <dt>evals</dt>
                <dd>{card.eval_signals.filter((signal) => signal.passed).length} pass / {card.eval_signals.filter((signal) => !signal.passed).length} fail</dd>
              </div>
              <div>
                <dt>failures</dt>
                <dd>{card.failure_history.length}</dd>
              </div>
            </dl>

            <section class="provenance" aria-label="Provenance">
              <h4>Provenance</h4>
              <dl>
                {#each card.provenance as pair}
                  <div>
                    <dt>{pair[0]}</dt>
                    <dd>{pair[1]}</dd>
                  </div>
                {/each}
              </dl>
            </section>

            <section class="dependencies" aria-label="Dependencies">
              <h4>Dependencies</h4>
              {#if card.dependencies.length === 0}
                <p>None recorded</p>
              {:else}
                <div class="chips">
                  {#each card.dependencies as dependency}
                    <button type="button" onclick={() => { selectedCardId = dependency; }}>{dependency}</button>
                  {/each}
                </div>
              {/if}
            </section>
          </article>
        {:else}
          <div class="state">No evidence asset cards matched the current filters.</div>
        {/each}
      </main>

      <aside class="detail-panel" aria-label="Evidence detail">
        {#if selectedCard}
          <h3>{selectedCard.name}</h3>
          <section>
            <h4>Recent runs</h4>
            {#each selectedCard.recent_runs as run}
              <div class="run-row">
                <span>{run.run_id}</span>
                <strong>{run.status}</strong>
                <time>{run.finished_at_utc}</time>
              </div>
            {:else}
              <p>No runs recorded.</p>
            {/each}
          </section>
          <section>
            <h4>Failure history</h4>
            {#each (failureHistoryDetail ?? selectedCard.failure_history) as failure}
              <div class="failure-row">
                <strong>{failure.kind}</strong>
                <p>{failure.summary}</p>
                <time>{failure.recorded_at_utc}</time>
              </div>
            {:else}
              <p>No failures recorded.</p>
            {/each}
          </section>
        {:else}
          <p>Select a card to inspect supporting evidence.</p>
        {/if}

        <section class="kind-catalog" aria-label="Evidence asset kinds">
          <h3>Kinds</h3>
          {#each kindCatalog as kind (kind.id)}
            <button type="button" class:active={filterKind === kind.id} aria-pressed={filterKind === kind.id} onclick={() => { filterKind = filterKind === kind.id ? null : kind.id; }}>
              <span>{kind.display_label}</span>
              <strong>{countsByKind[kind.id] ?? 0}</strong>
              <small>{kind.description}</small>
            </button>
          {/each}
        </section>
      </aside>
    </div>
  {/if}
</div>

<style>
  .evidence-assets { padding: 18px; max-width: 1500px; display: flex; flex-direction: column; gap: 14px; }
  .asset-header, .filters { display: flex; justify-content: space-between; gap: 14px; align-items: flex-start; flex-wrap: wrap; }
  h2, h3, h4, p, dl { margin: 0; }
  h2 { font-size: 1.25rem; color: var(--text-primary); }
  h3 { font-size: 1rem; color: var(--text-primary); }
  h4 { font-size: 0.78rem; text-transform: uppercase; color: var(--text-muted); }
  p, dd, dt, small { color: var(--text-muted); }
  .summary { display: flex; gap: 8px; color: var(--text-muted); font-size: 0.85rem; }
  .filters label { display: grid; gap: 4px; font-size: 0.78rem; color: var(--text-muted); }
  select { min-width: 150px; background: var(--surface-elevated); color: var(--text-primary); border: 1px solid var(--border-default); border-radius: 6px; padding: 6px 8px; }
  .asset-layout { display: grid; grid-template-columns: 190px minmax(420px, 1fr) minmax(280px, 360px); gap: 12px; align-items: start; }
  .maturity-lanes, .detail-panel, .kind-catalog { display: flex; flex-direction: column; gap: 8px; }
  .maturity-lanes button, .kind-catalog button { text-align: left; border: 1px solid var(--border-default); background: var(--surface-elevated); color: var(--text-primary); border-radius: 8px; padding: 9px; display: grid; gap: 4px; }
  .maturity-lanes button { grid-template-columns: 1fr auto; align-items: center; }
  button.active { border-color: var(--accent); }
  .card-list { display: grid; gap: 10px; }
  .asset-card, .detail-panel { border: 1px solid var(--border-default); background: var(--surface-elevated); border-radius: 8px; padding: 12px; }
  .asset-card { display: grid; gap: 12px; }
  .asset-card.selected { border-color: var(--accent); }
  .asset-card header { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
  .card-actions { display: flex; flex-wrap: wrap; gap: 6px; justify-content: flex-end; align-items: center; }
  .select-card { border: 1px solid var(--border-default); border-radius: 6px; padding: 4px 8px; background: var(--surface-default); color: var(--text-primary); }
  .kind { font-size: 0.75rem; color: var(--text-muted); text-transform: uppercase; }
  .proof { border-radius: 999px; padding: 4px 8px; font-size: 0.75rem; border: 1px solid var(--border-default); white-space: nowrap; }
  .proof.failed { color: var(--danger); }
  .proof.verified { color: var(--success); }
  .proof.unknown, .proof.unverified { color: var(--warning); }
  .facts { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
  .facts div, .provenance dl div, .run-row, .failure-row { border-top: 1px solid var(--border-default); padding-top: 7px; }
  dt { font-size: 0.72rem; text-transform: uppercase; }
  dd { overflow-wrap: anywhere; }
  .provenance dl { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin-top: 6px; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; }
  .chips button { border: 1px solid var(--border-default); border-radius: 999px; padding: 4px 8px; background: transparent; color: var(--text-primary); }
  .detail-panel section { display: grid; gap: 8px; }
  .run-row, .failure-row { display: grid; gap: 3px; }
  .state { padding: 32px; border: 1px solid var(--border-default); border-radius: 8px; color: var(--text-muted); background: var(--surface-elevated); }
  .error { color: var(--danger); }
  @media (max-width: 1150px) { .asset-layout { grid-template-columns: 1fr; } }
  @media (max-width: 680px) { .facts, .provenance dl { grid-template-columns: 1fr; } }
</style>
