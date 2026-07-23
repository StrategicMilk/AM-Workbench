<script>
  import { onMount } from 'svelte';
  import { appState } from '$lib/stores/app.svelte.js';
  import { workbenchKernelRequest } from '$lib/api.js';
  import { GatewayDecisionKind } from '$lib/contracts/enums.js';
  import { unwrapDecisions } from '$lib/contracts/unwrap.js';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';

  const DECISION_KINDS = Object.values(GatewayDecisionKind);
  const ACTION_STYLES = {
    block: 'danger',
    log: 'neutral',
    retry: 'warning',
    fallback: 'info',
    eval_dataset: 'purple',
    human_approval: 'orange',
  };

  let profiles = $state([]);
  let decisions = $state([]);
  let kindFilter = $state('');
  let loading = $state(false);
  let errorMessage = $state('');

  let projectId = $derived(appState.currentProjectId || 'default');
  let visibleDecisions = $derived(
    kindFilter ? decisions.filter((d) => d.inputs_summary?.startsWith(kindFilter)) : decisions
  );

  async function getJson(url) {
    try {
      return await workbenchKernelRequest(url);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      if (message.startsWith('401 ') || message.startsWith('403 ')) {
        throw new Error('Admin access required to view gateway policy.');
      }
      throw err;
    }
  }

  async function loadProfiles() {
    const data = await getJson(`/api/v1/workbench/${encodeURIComponent(projectId)}/gateway-policy/profiles`);
    profiles = Array.isArray(data?.profiles) ? data.profiles : [];
  }

  async function loadDecisions() {
    const params = new URLSearchParams({ limit: '200' });
    if (kindFilter) params.set('kind', kindFilter);
    const data = await getJson(
      `/api/v1/workbench/${encodeURIComponent(projectId)}/gateway-policy/decisions?${params.toString()}`
    );
    decisions = unwrapDecisions(data);
  }

  async function refresh() {
    loading = true;
    errorMessage = '';
    try {
      await Promise.all([loadProfiles(), loadDecisions()]);
    } catch (err) {
      errorMessage = err.message;
      profiles = [];
      decisions = [];
    } finally {
      loading = false;
    }
  }

  function actionFromReceipt(row) {
    const text = row?.outputs_summary || '';
    const match = text.match(/action=([^|]+)/);
    return match ? match[1] : 'log';
  }

  function reasonFromReceipt(row) {
    const text = row?.outputs_summary || '';
    const match = text.match(/reason=([^|]+)/);
    return match ? match[1] : text;
  }

  function profileFromReceipt(row) {
    const text = row?.inputs_summary || '';
    const match = text.match(/profile=([^|]+)/);
    return match ? match[1] : 'unknown';
  }

  function kindFromReceipt(row) {
    const text = row?.inputs_summary || '';
    const kind = DECISION_KINDS.find((candidate) => text.startsWith(candidate));
    return kind || 'route';
  }

  function tabIsVisible() {
    return typeof document === 'undefined' || document.visibilityState === 'visible';
  }

  function refreshWhenActive() {
    if (tabIsVisible()) {
      void refresh();
    }
  }

  $effect(() => {
    const activeProjectId = projectId;
    const activeKindFilter = kindFilter;
    if (activeProjectId || activeKindFilter !== undefined) {
      refreshWhenActive();
    }
  });

  onMount(() => {
    const handleVisibilityChange = () => refreshWhenActive();

    document.addEventListener('visibilitychange', handleVisibilityChange);
    window.addEventListener('focus', refreshWhenActive);
    window.addEventListener('pageshow', refreshWhenActive);
    window.addEventListener('online', refreshWhenActive);
    window.addEventListener('vetinari:gateway-policy-refresh', refreshWhenActive);
    return () => {
      document.removeEventListener('visibilitychange', handleVisibilityChange);
      window.removeEventListener('focus', refreshWhenActive);
      window.removeEventListener('pageshow', refreshWhenActive);
      window.removeEventListener('online', refreshWhenActive);
      window.removeEventListener('vetinari:gateway-policy-refresh', refreshWhenActive);
    };
  });
</script>

<svelte:head>
  <title>Gateway Policy Console</title>
</svelte:head>

<section class="gateway-policy-console" aria-labelledby="gateway-policy-title">
  <header class="view-header">
    <div>
      <h1 id="gateway-policy-title">Gateway Policy</h1>
      <p>Project {projectId} · refreshes on activation and policy changes</p>
    </div>
    <button class="refresh-button" onclick={refresh} disabled={loading} type="button">
      <i class="fas fa-sync-alt" class:spinning={loading}></i>
      <span>Refresh</span>
    </button>
  </header>

  {#if errorMessage}
    <div class="error-banner" role="alert">{errorMessage}</div>
  {/if}

  <section class="policy-section" aria-labelledby="profiles-heading">
    <h2 id="profiles-heading">Profiles</h2>
    <div class="profile-grid">
      {#each profiles as profile}
        <article class="profile-card">
          <h3>{profile.id}</h3>
          <dl>
            <div>
              <dt>Fallback</dt>
              <dd>{(profile.fallback_chain || []).join(' -> ') || 'none'}</dd>
            </div>
            <div>
              <dt>Timeouts</dt>
              <dd>{JSON.stringify(profile.timeouts || {})}</dd>
            </div>
            <div>
              <dt>Budget</dt>
              <dd>{JSON.stringify(profile.budget_caps || {})}</dd>
            </div>
            <div>
              <dt>Cache</dt>
              <dd>{JSON.stringify(profile.cache || {})}</dd>
            </div>
          </dl>
        </article>
      {:else}
        <p class="empty-state">No active profiles are loaded.</p>
      {/each}
    </div>
  </section>

  <section class="policy-section" aria-labelledby="decisions-heading">
    <div class="table-toolbar">
      <h2 id="decisions-heading">Decisions</h2>
      <label>
        <span>Kind</span>
        <select bind:value={kindFilter}>
          <option value="">All</option>
          {#each DECISION_KINDS as kind}
            <option value={kind}>{kind}</option>
          {/each}
        </select>
      </label>
    </div>

    <div class="action-legend" aria-label="Guardrail action legend">
      {#each ['block', 'log', 'retry', 'fallback', 'eval_dataset', 'human_approval'] as action}
        <span class={`chip ${ACTION_STYLES[action]}`}>{action}</span>
      {/each}
    </div>

    {#if visibleDecisions.length === 0 && !errorMessage}
      <p class="empty-state">No policy decisions in the recent receipt window.</p>
    {:else}
      <div class="decision-table-wrap">
        <table class="decision-table">
          <thead>
            <tr>
              <th>Time</th>
              <th>Kind</th>
              <th>Profile</th>
              <th>Action</th>
              <th>Reason</th>
              <th>Passed</th>
            </tr>
          </thead>
          <tbody>
            {#each visibleDecisions as row}
              {@const action = actionFromReceipt(row)}
              <tr>
                <td>{row.started_at_utc}</td>
                <td>{kindFromReceipt(row)}</td>
                <td>{profileFromReceipt(row)}</td>
                <td><span class={`chip ${ACTION_STYLES[action] || 'neutral'}`}>{action}</span></td>
                <td>{reasonFromReceipt(row)}</td>
                <td>
                  <span class:pass={row.outcome?.passed} class:fail={!row.outcome?.passed}>
                    {row.outcome?.passed ? 'true' : 'false'}
                  </span>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </section>
</section>

<style>
  .gateway-policy-console {
    display: flex;
    flex-direction: column;
    gap: 20px;
    padding: 24px;
  }

  .view-header,
  .table-toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
  }

  h1,
  h2,
  h3,
  p {
    margin: 0;
  }

  .view-header p {
    color: var(--text-muted);
    margin-top: 4px;
  }

  .refresh-button {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    border: 1px solid var(--border-default);
    background: var(--surface-elevated);
    color: var(--text-primary);
    padding: 8px 12px;
    border-radius: 6px;
    cursor: pointer;
  }

  .spinning {
    animation: spin 900ms linear infinite;
  }

  .error-banner {
    border: 1px solid #ef4444;
    background: rgba(239, 68, 68, 0.12);
    color: #fecaca;
    padding: 10px 12px;
    border-radius: 6px;
  }

  .policy-section {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .profile-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 12px;
  }

  .profile-card {
    border: 1px solid var(--border-default);
    border-radius: 8px;
    padding: 14px;
    background: var(--surface-elevated);
  }

  dl {
    display: grid;
    gap: 8px;
    margin: 12px 0 0;
  }

  dt {
    color: var(--text-muted);
    font-size: 12px;
  }

  dd {
    margin: 2px 0 0;
    overflow-wrap: anywhere;
  }

  select {
    background: var(--surface-elevated);
    color: var(--text-primary);
    border: 1px solid var(--border-default);
    border-radius: 6px;
    padding: 6px 8px;
  }

  .action-legend {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  .decision-table-wrap {
    overflow-x: auto;
    border: 1px solid var(--border-default);
    border-radius: 8px;
  }

  .decision-table {
    width: 100%;
    border-collapse: collapse;
    min-width: 760px;
  }

  th,
  td {
    text-align: left;
    padding: 10px 12px;
    border-bottom: 1px solid var(--border-default);
    vertical-align: top;
  }

  th {
    color: var(--text-muted);
    font-weight: 600;
  }

  .chip {
    display: inline-flex;
    align-items: center;
    border-radius: 999px;
    padding: 3px 8px;
    font-size: 12px;
    font-weight: 600;
    background: rgba(148, 163, 184, 0.16);
  }

  .danger { color: #fca5a5; }
  .neutral { color: #cbd5e1; }
  .warning { color: #fcd34d; }
  .info { color: #93c5fd; }
  .purple { color: #c4b5fd; }
  .orange { color: #fdba74; }

  .pass {
    color: #86efac;
  }

  .fail {
    color: #fca5a5;
  }

  .empty-state {
    color: var(--text-muted);
    padding: 10px 0;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }

  @media (max-width: 760px) {
    .gateway-policy-console {
      padding: 16px;
    }

    .view-header,
    .table-toolbar {
      align-items: stretch;
      flex-direction: column;
    }

    .refresh-button {
      justify-content: center;
      width: 100%;
    }

    .profile-grid {
      grid-template-columns: 1fr;
    }

    .decision-table {
      min-width: 640px;
    }
  }
</style>
