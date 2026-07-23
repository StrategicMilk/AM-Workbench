<script>
  import { showToast } from '$lib/stores/toast.svelte.js';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';
  import { workbenchKernelRequest } from '$lib/api.js';
  import { browserLocationParam } from '$lib/utils/browser.js';

  let { projectId = browserLocationParam('project_id', 'default') } = $props();

  let cards = $state([]);
  let methodCatalog = $state([]);
  let negativeMethods = $state([]);
  let selectedCardId = $state(null);
  let filterKind = $state(null);
  let filterPromotionStatus = $state(null);
  let filterTaskProfile = $state(null);
  let loading = $state(true);
  let error = $state(null);

  const promotionStatuses = ['not_promotable', 'measured_negative', 'measured_mixed', 'measured_positive', 'promoted'];

  let selectedCard = $derived(cards.find((card) => card.method_card_id === selectedCardId) ?? null);
  let cardsByPromotionStatus = $derived(
    promotionStatuses.reduce((acc, status) => {
      acc[status] = cards.filter((card) => card.promotion_status === status);
      return acc;
    }, {})
  );

  function catalogLabel(kind) {
    return methodCatalog.find((row) => row.id === kind)?.display_label ?? kind;
  }

  function latestNegativeSummary(card) {
    return card.evidence_refs?.find((ref) => ref.sign === 'negative')?.summary ?? 'Measured negative result recorded.';
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
      workbenchKernelRequest(workbenchUrl('/api/workbench/method-library', {
        project_id: projectId,
        kind: filterKind,
        promotion_status: filterPromotionStatus,
      })),
      workbenchKernelRequest('/api/workbench/method-library/catalog'),
      workbenchKernelRequest(workbenchUrl('/api/workbench/method-library/negative-methods', {
        project_id: projectId,
        task_profile: filterTaskProfile,
      })),
    ]).then(([cardRows, catalogRows, negativeRows]) => {
      if (cancelled) return;
      cards = Array.isArray(cardRows?.methods) ? cardRows.methods : Array.isArray(cardRows) ? cardRows : [];
      methodCatalog = Array.isArray(catalogRows?.catalog) ? catalogRows.catalog : Array.isArray(catalogRows) ? catalogRows : [];
      negativeMethods = Array.isArray(negativeRows?.negative_methods) ? negativeRows.negative_methods : Array.isArray(negativeRows) ? negativeRows : [];
      if (!selectedCardId && cards.length > 0) selectedCardId = cards[0].method_card_id;
      loading = false;
    }).catch((err) => {
      if (cancelled) return;
      error = err.message ?? String(err);
      showToast(`Method library load failed: ${error}`, 'error');
      loading = false;
    });
    return () => { cancelled = true; };
  });
</script>

<div class="method-library">
  <header class="library-header">
    <div>
      <h2>Method Library</h2>
      <p>{projectId}</p>
      <HelpPopover
        title="Method Library"
        body="Catalog of behavior methods discovered and measured by AM Workbench for this project. Each method card records when to use the method, when not to use it, expected cost, known failure modes, compatible task profiles, and measured performance deltas against a baseline. Promotion status tracks whether a method has been measured as positive, negative, or mixed and whether it has been promoted to the active method set. Negative methods are flagged separately as anti-patterns to avoid. Filter by kind or promotion status to focus on specific method categories."
        severity="info"
      />
    </div>
    <div class="filters" aria-label="Method filters">
      <select bind:value={filterKind} aria-label="Kind filter">
        <option value={null}>All kinds</option>
        {#each methodCatalog as row (row.id)}
          <option value={row.id}>{row.display_label}</option>
        {/each}
      </select>
      <select bind:value={filterPromotionStatus} aria-label="Promotion status filter">
        <option value={null}>All statuses</option>
        {#each promotionStatuses as status}
          <option value={status}>{status}</option>
        {/each}
      </select>
      <input bind:value={filterTaskProfile} placeholder="Task profile" aria-label="Negative method task profile" />
    </div>
  </header>

  {#if loading}
    <div class="state">Loading method cards.</div>
  {:else if error}
    <div class="state error">{error}</div>
  {:else}
    <section class="negative-lane" data-testid="negative-methods-lane" aria-label="Negative methods">
      <div class="lane-heading">
        <h3>Negative Methods</h3>
        <span>{negativeMethods.length}</span>
      </div>
      <div class="negative-grid">
        {#each negativeMethods as card (card.method_card_id)}
          <button class="negative-card" type="button" aria-pressed={selectedCardId === card.method_card_id} onclick={() => { selectedCardId = card.method_card_id; }}>
            <strong>{card.name}</strong>
            <small>{catalogLabel(card.kind)} · {card.kind}</small>
            <span>{latestNegativeSummary(card)}</span>
          </button>
        {/each}
      </div>
    </section>

    <div class="status-grid">
      {#each promotionStatuses as status}
        <section class="status-lane" data-testid={`promotion-status-lane-${status}`} aria-label={status}>
          <div class="lane-heading">
            <h3>{status}</h3>
            <span>{cardsByPromotionStatus[status]?.length ?? 0}</span>
          </div>
          {#each cardsByPromotionStatus[status] ?? [] as card (card.method_card_id)}
            <article
              class:selected={selectedCardId === card.method_card_id}
              aria-current={selectedCardId === card.method_card_id ? 'true' : undefined}
              class="method-card"
              data-testid={`method-card-${card.method_card_id}`}
            >
              <button type="button" aria-pressed={selectedCardId === card.method_card_id} onclick={() => { selectedCardId = card.method_card_id; }}>
                <strong>{card.name}</strong>
                <small>{catalogLabel(card.kind)} · {card.promotion_status}</small>
                <span>{card.description}</span>
              </button>
            </article>
          {/each}
        </section>
      {/each}
    </div>

    {#if selectedCard}
      <aside class="detail-pane" aria-label="Method card detail">
        <header>
          <h3>{selectedCard.name}</h3>
          <p>{selectedCard.kind} · {selectedCard.promotion_status}</p>
        </header>
        <p>{selectedCard.description}</p>
        <div class="detail-columns">
          <section>
            <h4>When to use</h4>
            <ul>{#each selectedCard.when_to_use as item}<li>{item}</li>{/each}</ul>
          </section>
          <section>
            <h4>When not to use</h4>
            <ul>{#each selectedCard.when_not_to_use as item}<li>{item}</li>{/each}</ul>
          </section>
        </div>
        <section>
          <h4>Expected cost</h4>
          <p>{selectedCard.expected_cost}</p>
        </section>
        <div class="detail-columns">
          <section>
            <h4>Known failure modes</h4>
            <ul>{#each selectedCard.known_failure_modes as item}<li>{item}</li>{/each}</ul>
          </section>
          <section>
            <h4>Compatible task profiles</h4>
            <ul>{#each selectedCard.compatible_task_profiles as item}<li>{item}</li>{/each}</ul>
          </section>
        </div>
        <section>
          <h4>Measured deltas</h4>
          <table>
            <thead>
              <tr><th>Metric</th><th>Baseline</th><th>Method</th><th>Delta</th><th>Sign</th></tr>
            </thead>
            <tbody>
              {#each selectedCard.measured_deltas as delta}
                <tr class={`delta-${delta.sign}`}>
                  <td>{delta.metric_name}</td>
                  <td>{delta.baseline_value}</td>
                  <td>{delta.method_value}</td>
                  <td>{delta.delta}</td>
                  <td>{delta.sign}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </section>
        <section>
          <h4>Evidence refs</h4>
          <ul>
            {#each selectedCard.evidence_refs as ref}
              <li>{ref.summary} ({ref.sign})</li>
            {/each}
          </ul>
        </section>
      </aside>
    {/if}
  {/if}
</div>

<style>
  .method-library { padding: 18px; max-width: 1440px; display: flex; flex-direction: column; gap: 14px; color: var(--text-primary); }
  .library-header { display: flex; justify-content: space-between; gap: 14px; align-items: flex-start; flex-wrap: wrap; }
  h2, h3, h4, p { margin: 0; }
  h2 { font-size: 1.25rem; }
  h3 { font-size: 0.95rem; }
  h4 { font-size: 0.82rem; margin-bottom: 6px; }
  p, li, td, th, span, small { font-size: 0.82rem; }
  .library-header p { color: var(--text-muted); font-family: var(--font-mono); margin-top: 3px; }
  .filters { display: flex; gap: 8px; flex-wrap: wrap; }
  select, input { min-height: 44px; border: 1px solid var(--border-default); border-radius: 6px; background: var(--surface-elevated); color: var(--text-primary); padding: 0 8px; }
  .state { padding: 32px; border: 1px solid var(--border-default); border-radius: 8px; color: var(--text-muted); background: var(--surface-elevated); }
  .error { color: var(--danger); }
  .negative-lane, .status-lane, .detail-pane { border: 1px solid var(--border-default); border-radius: 8px; background: var(--surface-elevated); padding: 12px; }
  .lane-heading { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
  .negative-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 8px; }
  .negative-card, .method-card button { width: 100%; min-height: 44px; text-align: left; border: 1px solid var(--border-default); border-radius: 6px; background: var(--surface-default); color: var(--text-primary); padding: 10px; display: flex; flex-direction: column; gap: 5px; }
  .negative-card small, .method-card small { color: var(--text-muted); font-family: var(--font-mono); }
  .status-grid { display: grid; grid-template-columns: repeat(5, minmax(180px, 1fr)); gap: 10px; align-items: start; }
  .status-lane { min-height: 140px; }
  .method-card { margin-bottom: 8px; }
  .method-card.selected button { border-color: var(--accent); }
  .detail-pane { display: flex; flex-direction: column; gap: 12px; }
  .detail-pane header p { color: var(--text-muted); font-family: var(--font-mono); }
  .detail-columns { display: grid; grid-template-columns: repeat(2, minmax(240px, 1fr)); gap: 12px; }
  ul { margin: 0; padding-left: 18px; }
  table { width: 100%; border-collapse: collapse; }
  th, td { border-bottom: 1px solid var(--border-default); padding: 6px; text-align: left; }
  .delta-positive td:last-child { color: var(--success); }
  .delta-negative td:last-child { color: var(--danger); }
  .delta-neutral td:last-child { color: var(--text-muted); }
  @media (max-width: 1100px) {
    .status-grid, .detail-columns { grid-template-columns: 1fr; }
  }
</style>
