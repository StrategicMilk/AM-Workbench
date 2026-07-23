<script>
  import { focusTrap } from '$lib/a11y/focusTrap.js';
  import { workbenchKernelRequest } from '$lib/api.js';
  import { errorMessage } from '$lib/utils/safe.js';

  let { open = false, record = null, projectId = 'default', onClose = () => {} } = $props();
  let evals = $state([]);
  let proposals = $state([]);
  let leases = $state([]);
  let loading = $state(false);
  let error = $state(null);
  let retryNonce = $state(0);

  function query(path, params) {
    const search = new URLSearchParams(params);
    return workbenchKernelRequest(`${path}?${search.toString()}`);
  }

  function rowsFromResponse(value, key) {
    if (Array.isArray(value)) return value;
    if (Array.isArray(value?.[key])) return value[key];
    if (Array.isArray(value?.items)) return value.items;
    if (Array.isArray(value?.rows)) return value.rows;
    return [];
  }

  $effect(() => {
    void retryNonce;
    if (!open || !record) return;
    let cancelled = false;
    loading = true;
    error = null;
    const params = { project_id: projectId, limit: '25' };
    if (record.run_id) params.run_id = record.run_id;
    if (record.asset_id) params.asset_id = record.asset_id;
    Promise.all([
      query('/api/workbench/console/evals', params),
      query('/api/workbench/console/proposals', { project_id: projectId, limit: '25' }),
      query('/api/workbench/console/leases', { project_id: projectId, run_id: record.run_id ?? '' }),
    ]).then(([evalRows, proposalRows, leaseRows]) => {
      if (cancelled) return;
      evals = rowsFromResponse(evalRows, 'evals');
      proposals = rowsFromResponse(proposalRows, 'proposals');
      leases = rowsFromResponse(leaseRows, 'leases');
      loading = false;
    }).catch((err) => {
      if (cancelled) return;
      error = errorMessage(err);
      loading = false;
    });
    return () => { cancelled = true; };
  });
</script>

{#if open}
  <div
    class="drawer"
    data-testid="artifact-drawer"
    role="dialog"
    aria-modal="true"
    aria-labelledby="artifact-drawer-title"
    use:focusTrap
    onescape={onClose}
  >
    <div class="drawer-head">
      <h3 id="artifact-drawer-title">Evidence</h3>
      <button type="button" onclick={onClose} aria-label="Close evidence drawer">close</button>
    </div>
    {#if loading}
      <p>Loading evidence.</p>
    {:else if error}
      <p class="error">{error}</p>
      <button type="button" onclick={() => { retryNonce += 1; }}>retry</button>
    {:else}
      <section>
        <h4>Evals</h4>
        {#each evals as item (item.eval_id)}
          <p data-testid="eval-row-{item.eval_id}">{item.eval_id} {item.kind}</p>
        {:else}
          <p class="quiet">No evals returned.</p>
        {/each}
      </section>
      <section>
        <h4>Proposals</h4>
        {#each proposals as item (item.proposal_id)}
          <p>{item.proposal_id} {item.status}</p>
        {:else}
          <p class="quiet">No proposals returned.</p>
        {/each}
      </section>
      <section>
        <h4>Leases</h4>
        {#each leases as item (item.lease_id)}
          <p>{item.lease_id} {item.status}</p>
        {:else}
          <p class="quiet">No leases returned.</p>
        {/each}
      </section>
    {/if}
  </div>
{/if}

<style>
  .drawer { position: fixed; right: 0; top: 0; bottom: 0; width: min(420px, 100vw); padding: 18px; background: var(--surface-elevated); border-left: 1px solid var(--border-default); box-shadow: var(--shadow-lg); z-index: 30; overflow: auto; }
  .drawer-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
  h3, h4, p { margin: 0; }
  h3 { color: var(--text-primary); }
  h4 { margin-top: 16px; color: var(--text-muted); font-size: 0.76rem; text-transform: uppercase; }
  p { padding: 6px 0; color: var(--text-secondary); font-size: 0.82rem; }
  button { border: 1px solid var(--border-default); border-radius: 6px; background: var(--surface-hover); color: var(--text-primary); font: inherit; min-height: 44px; padding: 6px 9px; cursor: pointer; }
  .quiet { color: var(--text-muted); }
  .error { color: var(--danger); }
</style>
