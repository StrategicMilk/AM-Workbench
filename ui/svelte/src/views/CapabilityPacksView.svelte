<script>
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';
  import { workbenchKernelRequest } from '$lib/api.js';
  import ProvenanceGate from '$lib/security/ProvenanceGate.svelte';
  import { provenanceDecision, redactSupplyChainValue, requireTrustedProvenance } from '$lib/security';

  const TRUST_FILTERS = ['all', 'trusted', 'denied', 'degraded'];
  const ACTIONS = ['enable', 'disable', 'smoke-test', 'uninstall'];

  let loading = $state(true);
  let actionPending = $state('');
  let errorMessage = $state('');
  let packs = $state([]);
  let trustFilter = $state('all');
  let query = $state('');

  let visiblePacks = $derived(
    packs.filter((pack) => {
      const status = pack.enablement?.status ?? 'degraded';
      const matchesStatus = trustFilter === 'all' || status === trustFilter;
      const haystack = `${pack.pack_id} ${pack.capability_kind} ${pack.source}`.toLowerCase();
      return matchesStatus && haystack.includes(query.toLowerCase());
    })
  );

  async function request(url, options = {}) {
    const method = (options.method ?? 'GET').toUpperCase();
    const csrfHeaders = method === 'GET' ? {} : { 'X-Requested-With': 'XMLHttpRequest' };
    return workbenchKernelRequest(url, {
      headers: { 'Content-Type': 'application/json', ...csrfHeaders, ...options.headers },
      ...options,
    });
  }

  async function loadPacks() {
    loading = true;
    errorMessage = '';
    try {
      const data = await request('/api/workbench/capability-packs');
      packs = Array.isArray(data?.packs) ? data.packs : [];
    } catch (err) {
      errorMessage = err.message;
      packs = [];
    } finally {
      loading = false;
    }
  }

  async function runAction(packId, action) {
    if (action === 'disable' || action === 'uninstall') {
      const confirmed = confirm(
        `${action === 'uninstall' ? 'Uninstall' : 'Disable'} capability pack "${packId}"? This can interrupt workflows that depend on it.`
      );
      if (!confirmed) return;
    }
    actionPending = `${packId}:${action}`;
    errorMessage = '';
    try {
      const pack = packs.find((item) => item.pack_id === packId);
      if (action === 'enable' || action === 'smoke-test') {
        requireTrustedProvenance(packProvenanceInput(pack), `capability-pack:${packId}:${action}`);
      }
      await request(`/api/workbench/capability-packs/${encodeURIComponent(packId)}/${action}`, {
        method: 'POST',
      });
      await loadPacks();
    } catch (err) {
      errorMessage = err.message;
    } finally {
      actionPending = '';
    }
  }

  function canRun(pack, action) {
    const actions = pack.enablement?.actions ?? {};
    const key = action === 'smoke-test' ? 'smoke_test' : action;
    const requiresTrust = action === 'enable' || action === 'smoke-test';
    const decision = provenanceDecision(packProvenanceInput(pack), `capability-pack:${pack?.pack_id ?? 'unknown'}`);
    return Boolean(actions[key]) && (!requiresTrust || decision.trusted);
  }

  function packProvenanceInput(pack) {
    return {
      evidence_refs: [
        ...(Array.isArray(pack?.evidence_refs) ? pack.evidence_refs : []),
        ...(Array.isArray(pack?.policy_bindings) ? pack.policy_bindings.map((policy) => `policy:${policy}`) : []),
        ...(Array.isArray(pack?.smoke_evals) ? pack.smoke_evals.map((smoke) => `test:${smoke}`) : []),
        pack?.source ? `source:${pack.source}` : null,
        pack?.version ? `version:${pack.version}` : null,
      ].filter(Boolean),
      status: pack?.enablement?.status ?? pack?.trust_status ?? pack?.current_status,
      allowed: pack?.enablement?.allowed,
      reasons: pack?.enablement?.reasons,
    };
  }

  function denialText(pack) {
    const reasons = pack.enablement?.reasons ?? [];
    return reasons.length > 0 ? reasons.join('; ') : 'No denial reason reported';
  }

  function listText(values) {
    return Array.isArray(values) && values.length > 0 ? values.join(', ') : 'none';
  }

  $effect(() => { loadPacks(); });
</script>

<section class="capability-packs-view" aria-labelledby="capability-packs-title">
  <header class="view-header">
    <div>
      <h1 id="capability-packs-title">Capability Packs</h1>
      <p>{packs.length} trusted installable units inspected</p>
    </div>
    <button class="tool-button" onclick={loadPacks} disabled={loading} type="button" title="Refresh">
      <i class="fas fa-sync-alt" class:fa-spin={loading} aria-hidden="true"></i>
      <span>Refresh</span>
    </button>
  </header>

  <div class="toolbar" aria-label="Capability pack filters">
    <label>
      <span>Filter</span>
      <select bind:value={trustFilter}>
        {#each TRUST_FILTERS as filter}
          <option value={filter}>{filter}</option>
        {/each}
      </select>
    </label>
    <label class="search-box">
      <span>Search</span>
      <input bind:value={query} type="search" placeholder="pack id, kind, source" />
    </label>
  </div>

  {#if errorMessage}
    <div class="error-banner" role="alert">{errorMessage}</div>
  {/if}

  {#if loading}
    <div class="loading-state" role="status">
      <i class="fas fa-spinner fa-spin" aria-hidden="true"></i>
      Loading capability packs
    </div>
  {:else if visiblePacks.length === 0}
    <p class="empty-state" role="status">No capability packs match the current filters.</p>
  {:else}
    <div class="pack-table-wrap">
      <table class="pack-table">
        <thead>
          <tr>
            <th>Pack</th>
            <th>Trust</th>
            <th>Policy</th>
            <th>Runtime</th>
            <th>Proof</th>
            <th>Limits</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {#each visiblePacks as pack (pack.pack_id)}
            {@const status = pack.enablement?.status ?? 'degraded'}
            <tr>
              <td>
                <strong>{pack.pack_id}</strong>
                <span>{pack.version} · {pack.capability_kind}</span>
                <small>{pack.source}</small>
              </td>
              <td>
                <span class={`status-pill ${status}`}>{status}</span>
                <p>{denialText(pack)}</p>
              </td>
              <td>
                <dl>
                  <div><dt>Policy</dt><dd>{listText(pack.policy_bindings)}</dd></div>
                  <div><dt>Cost</dt><dd>{pack.cost_policy}</dd></div>
                  <div><dt>Freshness</dt><dd>{pack.freshness_policy}</dd></div>
                </dl>
              </td>
              <td>
                <dl>
                  <div><dt>Current</dt><dd>{pack.current_status}</dd></div>
                  <div><dt>Tested</dt><dd>{pack.tested_status}</dd></div>
                  <div><dt>Locality</dt><dd>{pack.locality}</dd></div>
                  <div><dt>Credentials</dt><dd>{pack.credential_posture}</dd></div>
                </dl>
              </td>
              <td>
                <dl>
                  <div><dt>Schemas</dt><dd>{listText(pack.schemas)}</dd></div>
                  <div><dt>Smoke</dt><dd>{listText(pack.smoke_evals)}</dd></div>
                </dl>
                <ProvenanceGate
                  refs={packProvenanceInput(pack).evidence_refs}
                  status={packProvenanceInput(pack).status}
                  allowed={packProvenanceInput(pack).allowed}
                  reasons={packProvenanceInput(pack).reasons}
                  context={`capability-pack:${pack.pack_id}`}
                  compact
                />
              </td>
              <td>
                <strong>Known limitations</strong>
                <ul>
                  {#each pack.known_limitations ?? [] as limitation}
                    <li>{limitation}</li>
                  {/each}
                </ul>
                <small>Disable: {redactSupplyChainValue(pack.disable_command, 'command')}</small>
                <small>Uninstall: {redactSupplyChainValue(pack.uninstall_command, 'command')}</small>
              </td>
              <td>
                <div class="action-stack">
                  {#each ACTIONS as action}
                    <button
                      class="action-button"
                      onclick={() => runAction(pack.pack_id, action)}
                      disabled={!canRun(pack, action) || actionPending !== ''}
                      type="button"
                      title={canRun(pack, action) ? action : denialText(pack)}
                    >
                      {actionPending === `${pack.pack_id}:${action}` ? 'Working' : action}
                    </button>
                  {/each}
                </div>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</section>

<style>
  .capability-packs-view {
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
  p,
  dl,
  ul {
    margin: 0;
  }

  .view-header p,
  small,
  dt,
  .empty-state {
    color: var(--text-muted);
  }

  .tool-button,
  .action-button,
  select,
  input {
    background: var(--surface-elevated);
    border: 1px solid var(--border-default);
    border-radius: 6px;
    color: var(--text-primary);
  }

  .tool-button,
  .action-button {
    align-items: center;
    cursor: pointer;
    display: inline-flex;
    gap: 8px;
    justify-content: center;
    min-height: 44px;
    padding: 7px 10px;
  }

  .action-button:disabled,
  .tool-button:disabled {
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

  select,
  input {
    min-height: 44px;
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
    padding: 10px 12px;
  }

  .loading-state {
    align-items: center;
    color: var(--text-muted);
    display: flex;
    gap: 10px;
  }

  .pack-table-wrap {
    border: 1px solid var(--border-default);
    border-radius: 8px;
    overflow-x: auto;
  }

  .pack-table {
    border-collapse: collapse;
    min-width: 1120px;
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

  td:first-child {
    min-width: 180px;
  }

  td:first-child,
  dl,
  .action-stack {
    display: grid;
    gap: 8px;
  }

  dd {
    margin: 1px 0 0;
    overflow-wrap: anywhere;
  }

  ul {
    display: grid;
    gap: 5px;
    padding-left: 18px;
  }

  .status-pill {
    border-radius: 999px;
    display: inline-flex;
    font-size: 12px;
    font-weight: 700;
    padding: 4px 8px;
    text-transform: uppercase;
  }

  .trusted {
    background: rgba(22, 163, 74, 0.14);
    color: #86efac;
  }

  .denied {
    background: rgba(220, 38, 38, 0.14);
    color: #fca5a5;
  }

  .degraded {
    background: rgba(234, 179, 8, 0.14);
    color: #fde68a;
  }

  .action-stack {
    min-width: 120px;
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
