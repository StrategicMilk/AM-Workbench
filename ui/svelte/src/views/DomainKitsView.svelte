<script>
  import { workbenchKernelRequest } from '$lib/api.js';
  import Icon from '$lib/a11y/Icon.svelte';

  let loading = $state(true);
  let errorMessage = $state('');
  let kits = $state([]);
  let selectedKitId = $state('');
  let domainFilter = $state('all');
  let query = $state('');
  let requestedWorkflow = $state('');
  let requestedClaimKind = $state('');
  let caveatsAcknowledged = $state(false);
  let verdict = $state(null);
  let evaluating = $state(false);

  let domainFilters = $derived(['all', ...new Set(kits.map((kit) => kit.domain).filter(Boolean).sort())]);
  let selectedKit = $derived(kits.find((kit) => kit.kit_id === selectedKitId) ?? kits[0] ?? null);
  let visibleKits = $derived(
    kits.filter((kit) => {
      const matchesDomain = domainFilter === 'all' || kit.domain === domainFilter;
      const haystack = `${kit.kit_id} ${kit.title} ${kit.domain} ${(kit.supported_workflows ?? []).join(' ')}`.toLowerCase();
      return matchesDomain && haystack.includes(query.toLowerCase());
    })
  );

  function listText(values) {
    return Array.isArray(values) && values.length > 0 ? values.join(', ') : 'none';
  }

  function rateText(policy) {
    if (!policy) return 'none';
    const rpm = policy.requests_per_minute ?? 'unknown';
    const burst = policy.burst ? ` / burst ${policy.burst}` : '';
    return `${rpm}/min${burst}`;
  }

  async function requestJson(path, options = {}) {
    const method = (options.method ?? 'GET').toUpperCase();
    const csrfHeaders = method === 'GET' ? {} : { 'X-Requested-With': 'XMLHttpRequest' };
    return workbenchKernelRequest(path, {
      headers: { 'Content-Type': 'application/json', ...csrfHeaders, ...options.headers },
      ...options,
    });
  }

  function normalizeKitList(data) {
    if (!Array.isArray(data?.kits)) {
      throw new Error('domain kits response must contain a kits array');
    }
    return data.kits.filter((kit) => kit && typeof kit === 'object' && kit.kit_id && kit.domain);
  }

  async function loadKits() {
    loading = true;
    errorMessage = '';
    try {
      const data = await requestJson('/api/workbench/domain-kits');
      kits = normalizeKitList(data);
      if (!selectedKitId && kits.length > 0) selectedKitId = kits[0].kit_id;
    } catch (err) {
      errorMessage = err.message ?? String(err);
      kits = [];
    } finally {
      loading = false;
    }
  }

  async function evaluateSupport() {
    if (!selectedKit) return;
    evaluating = true;
    verdict = null;
    errorMessage = '';
    try {
      const caveatList = caveatsAcknowledged ? selectedKit.required_caveat_acknowledgements ?? [] : [];
      const data = await requestJson(`/api/workbench/domain-kits/${encodeURIComponent(selectedKit.kit_id)}/evaluate`, {
        method: 'POST',
        body: JSON.stringify({
          requested_workflow: requestedWorkflow,
          requested_claim_kind: requestedClaimKind,
          caveat_acknowledgements: caveatList,
        }),
      });
      if (!data?.verdict || typeof data.verdict !== 'object') {
        throw new Error('domain kit evaluation response must contain a verdict object');
      }
      verdict = data.verdict;
    } catch (err) {
      errorMessage = err.message ?? String(err);
    } finally {
      evaluating = false;
    }
  }

  function selectKit(kit) {
    selectedKitId = kit.kit_id;
    requestedWorkflow = kit.supported_workflows?.[0] ?? '';
    requestedClaimKind = kit.supported_claim_kinds?.[0] ?? '';
    caveatsAcknowledged = false;
    verdict = null;
  }

  $effect(() => { loadKits(); });

  $effect(() => {
    if (selectedKit && !requestedWorkflow) requestedWorkflow = selectedKit.supported_workflows?.[0] ?? '';
    if (selectedKit && !requestedClaimKind) requestedClaimKind = selectedKit.supported_claim_kinds?.[0] ?? '';
  });

  $effect(() => {
    if (!domainFilters.includes(domainFilter)) domainFilter = 'all';
  });
</script>

<section class="domain-kits-view" aria-labelledby="domain-kits-title">
  <header class="view-header">
    <div>
      <h1 id="domain-kits-title">Domain Kits</h1>
      <p>{kits.length} evidence-disciplined workflow bundles</p>
    </div>
    <button class="tool-button" onclick={loadKits} disabled={loading} type="button" title="Refresh domain kits" aria-label="Refresh domain kits">
      <Icon name="sync-alt" class={loading ? 'fa-spin' : ''} />
      <span>Refresh</span>
    </button>
  </header>

  <div class="toolbar" aria-label="Domain kit filters">
    <label>
      <span>Domain</span>
      <select bind:value={domainFilter}>
        {#each domainFilters as filter}
          <option value={filter}>{filter}</option>
        {/each}
      </select>
    </label>
    <label class="search-box">
      <span>Search</span>
      <input bind:value={query} type="search" placeholder="kit, workflow, domain" />
    </label>
  </div>

  {#if errorMessage}
    <div class="error-banner" role="alert" aria-live="assertive">
      <strong>Domain kit request failed.</strong>
      <span>{errorMessage}</span>
      <small>Refresh the kit list, then verify the Workbench API is running if the message repeats.</small>
    </div>
  {/if}

  {#if loading}
    <div class="loading-state" role="status" aria-live="polite">
      <Icon name="spinner" class="fa-spin" />
      Loading domain kits
    </div>
  {:else if visibleKits.length === 0}
    <p class="empty-state" role="status" aria-live="polite">
      No domain kits match the current filters. Clear search or switch the domain filter to all.
    </p>
  {:else}
    <div class="kit-layout">
      <div class="kit-table-wrap">
        <table class="kit-table">
          <thead>
            <tr>
              <th>Kit</th>
              <th>Workflows</th>
              <th>Evidence Inputs</th>
              <th>Eval And Rate</th>
              <th>Refusal Boundaries</th>
            </tr>
          </thead>
          <tbody>
            {#each visibleKits as kit (kit.kit_id)}
              <tr class:selected={selectedKit?.kit_id === kit.kit_id}>
                <td>
                  <button
                    class="kit-select"
                    type="button"
                    aria-pressed={selectedKit?.kit_id === kit.kit_id}
                    aria-label={`Select domain kit ${kit.title}`}
                    onclick={() => selectKit(kit)}
                  >
                    <strong>{kit.title}</strong>
                    <span>{kit.kit_id}</span>
                    <small>{kit.domain}</small>
                  </button>
                </td>
                <td>
                  <strong>{listText(kit.supported_workflows)}</strong>
                  <small>Claims: {listText(kit.supported_claim_kinds)}</small>
                  <small>Unsupported: {listText(kit.unsupported_claims)}</small>
                </td>
                <td>
                  <small>Capability packs: {listText(kit.capability_pack_ids)}</small>
                  <small>Source cards: {listText(kit.source_kinds)}</small>
                  <small>Tool cards: {listText(kit.tool_kinds)}</small>
                  <small>Benchmarks: {listText(kit.benchmark_provider_ids)}</small>
                </td>
                <td>
                  <small>Eval fixtures: {listText(kit.eval_fixtures)}</small>
                  <small>Rate: {rateText(kit.rate_limit_policy)}</small>
                  <small>Notebooks: {listText(kit.sample_notebook_refs)}</small>
                </td>
                <td>
                  <ul>
                    {#each kit.refusal_boundaries ?? [] as boundary}
                      <li>{boundary}</li>
                    {/each}
                  </ul>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>

      <aside class="evaluation-panel" aria-label="Support evaluation">
        <div>
          <h2>Support Evaluation</h2>
          <p>{selectedKit ? selectedKit.title : 'No kit selected'}</p>
        </div>
        {#if selectedKit}
          <label>
            <span>Workflow</span>
            <input bind:value={requestedWorkflow} list="domain-kit-workflows" />
          </label>
          <datalist id="domain-kit-workflows">
            {#each selectedKit.supported_workflows ?? [] as workflow}
              <option value={workflow}></option>
            {/each}
          </datalist>
          <label>
            <span>Claim kind</span>
            <input bind:value={requestedClaimKind} list="domain-kit-claims" />
          </label>
          <datalist id="domain-kit-claims">
            {#each [...(selectedKit.supported_claim_kinds ?? []), ...(selectedKit.unsupported_claims ?? [])] as claim}
              <option value={claim}></option>
            {/each}
          </datalist>
          <label class="check">
            <input type="checkbox" bind:checked={caveatsAcknowledged} />
            <span>Caveats acknowledged</span>
          </label>
          <button class="evaluate-button" type="button" onclick={evaluateSupport} disabled={evaluating}>
            {evaluating ? 'Evaluating' : 'Evaluate'}
          </button>
          <div class="required-caveats">
            <strong>Required caveats</strong>
            <small>{listText(selectedKit.required_caveat_acknowledgements)}</small>
          </div>
        {/if}
        {#if verdict}
          <div class:pass={verdict.supported} class:deny={!verdict.supported} class="verdict">
            <strong>{verdict.status}</strong>
            {#each verdict.reasons ?? [] as reason}
              <span>{reason}</span>
            {/each}
            {#if verdict.missing_caveat_acknowledgements?.length}
              <span>Missing caveats: {listText(verdict.missing_caveat_acknowledgements)}</span>
            {/if}
          </div>
        {:else}
          <div class="verdict muted">No evaluation yet.</div>
        {/if}
      </aside>
    </div>
  {/if}
</section>

<style>
  .domain-kits-view {
    display: flex;
    flex-direction: column;
    gap: 18px;
    padding: 24px;
  }

  .view-header,
  .toolbar {
    align-items: center;
    display: flex;
    gap: 16px;
    justify-content: space-between;
  }

  h1,
  h2,
  p,
  ul {
    margin: 0;
  }

  h1 {
    font-size: 1.4rem;
  }

  h2 {
    font-size: 1rem;
  }

  p,
  small,
  .empty-state,
  .muted {
    color: var(--text-muted);
  }

  .tool-button,
  .evaluate-button,
  .kit-select,
  select,
  input {
    background: var(--surface-elevated);
    border: 1px solid var(--border-default);
    border-radius: 6px;
    color: var(--text-primary);
  }

  .tool-button,
  .evaluate-button {
    align-items: center;
    cursor: pointer;
    display: inline-flex;
    gap: 8px;
    justify-content: center;
    min-height: 34px;
    padding: 7px 10px;
  }

  .tool-button:disabled,
  .evaluate-button:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }

  .toolbar {
    justify-content: flex-start;
  }

  label {
    display: grid;
    gap: 4px;
  }

  .check {
    align-items: center;
    display: flex;
    flex-direction: row;
  }

  select,
  input {
    min-height: 34px;
    padding: 7px 9px;
  }

  .search-box {
    min-width: min(360px, 100%);
  }

  .error-banner {
    background: rgba(220, 38, 38, 0.12);
    border: 1px solid #dc2626;
    border-radius: 6px;
    color: #fecaca;
    display: grid;
    gap: 4px;
    padding: 10px 12px;
  }

  .loading-state {
    align-items: center;
    color: var(--text-muted);
    display: flex;
    gap: 10px;
  }

  .kit-layout {
    align-items: start;
    display: grid;
    gap: 16px;
    grid-template-columns: minmax(0, 1fr) minmax(280px, 360px);
  }

  .kit-table-wrap,
  .evaluation-panel {
    border: 1px solid var(--border-default);
    border-radius: 8px;
    overflow-x: auto;
  }

  .kit-table {
    border-collapse: collapse;
    min-width: 1160px;
    width: 100%;
  }

  th,
  td {
    border-bottom: 1px solid var(--border-default);
    padding: 12px;
    text-align: left;
    vertical-align: top;
  }

  th {
    color: var(--text-muted);
    font-weight: 600;
  }

  tr.selected {
    background: rgba(59, 130, 246, 0.08);
  }

  td,
  .kit-select,
  .evaluation-panel,
  .verdict,
  .required-caveats {
    display: grid;
    gap: 8px;
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .kit-select {
    cursor: pointer;
    padding: 10px;
    text-align: left;
    width: 100%;
  }

  ul {
    display: grid;
    gap: 5px;
    padding-left: 18px;
  }

  .evaluation-panel {
    background: var(--surface-elevated);
    padding: 14px;
    position: sticky;
    top: 12px;
  }

  .verdict {
    border: 1px solid var(--border-default);
    border-radius: 6px;
    padding: 10px;
  }

  .pass {
    background: rgba(22, 163, 74, 0.12);
    color: #86efac;
  }

  .deny {
    background: rgba(220, 38, 38, 0.12);
    color: #fca5a5;
  }

  @media (max-width: 980px) {
    .kit-layout {
      grid-template-columns: 1fr;
    }

    .evaluation-panel {
      position: static;
    }
  }

  @media (max-width: 760px) {
    .view-header,
    .toolbar {
      align-items: stretch;
      flex-direction: column;
    }

    .tool-button {
      width: 100%;
    }
  }
</style>
